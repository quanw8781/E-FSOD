# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import logging
import numpy as np
from typing import Dict
import torch
import torch.nn.functional as F
from torch import nn

import detectron2.utils.comm as comm
from detectron2.layers import ShapeSpec
from detectron2.structures import Boxes, Instances, pairwise_iou
from detectron2.utils.events import get_event_storage, EventStorage
from detectron2.utils.registry import Registry

from ..backbone.resnet import BottleneckBlock, make_stage
from ..box_regression import Box2BoxTransform
from ..contrastive_loss import ContrastiveHead, SupConLoss, SupConLossV2
from ..matcher import Matcher
from ..poolers import ROIPooler
from ..proposal_generator.proposal_utils import add_ground_truth_to_proposals
from ..sampling import subsample_labels
from .box_head import build_box_head
from .meta_head import build_meta_head
from .fast_rcnn import build_predictor, FastRCNNOutputs, FastRCNNContrastOutputs
from .keypoint_head import build_keypoint_head, keypoint_rcnn_inference, keypoint_rcnn_loss
from .mask_head import build_mask_head, mask_rcnn_inference, mask_rcnn_loss

ROI_HEADS_REGISTRY = Registry("ROI_HEADS")
ROI_HEADS_REGISTRY.__doc__ = """
Registry for ROI heads in a generalized R-CNN model.
ROIHeads take feature maps and region proposals, and
perform per-region computation.

The registered object will be called with `obj(cfg, input_shape)`.
The call is expected to return an :class:`ROIHeads`.
"""

logger = logging.getLogger(__name__)


def build_roi_heads(cfg, input_shape):
    """
    Build ROIHeads defined by `cfg.MODEL.ROI_HEADS.NAME`.
    """
    name = cfg.MODEL.ROI_HEADS.NAME
    return ROI_HEADS_REGISTRY.get(name)(cfg, input_shape)


def select_foreground_proposals(proposals, bg_label):
    """
    Given a list of N Instances (for N images), each containing a `gt_classes` field,
    return a list of Instances that contain only instances with `gt_classes != -1 &&
    gt_classes != bg_label`.

    Args:
        proposals (list[Instances]): A list of N Instances, where N is the number of
            images in the batch.
        bg_label: label index of background class.

    Returns:
        list[Instances]: N Instances, each contains only the selected foreground instances.
        list[Tensor]: N boolean vector, correspond to the selection mask of
            each Instances object. True for selected instances.
    """
    assert isinstance(proposals, (list, tuple))
    assert isinstance(proposals[0], Instances)
    assert proposals[0].has("gt_classes")
    fg_proposals = []
    fg_selection_masks = []
    for proposals_per_image in proposals:
        gt_classes = proposals_per_image.gt_classes
        fg_selection_mask = (gt_classes != -1) & (gt_classes != bg_label)
        fg_idxs = fg_selection_mask.nonzero().squeeze(1)
        fg_proposals.append(proposals_per_image[fg_idxs])
        fg_selection_masks.append(fg_selection_mask)
    return fg_proposals, fg_selection_masks


def select_proposals_with_visible_keypoints(proposals):
    """
    Args:
        proposals (list[Instances]): a list of N Instances, where N is the
            number of images.

    Returns:
        proposals: only contains proposals with at least one visible keypoint.

    Note that this is still slightly different from Detectron.
    In Detectron, proposals for training keypoint head are re-sampled from
    all the proposals with IOU>threshold & >=1 visible keypoint.

    Here, the proposals are first sampled from all proposals with
    IOU>threshold, then proposals with no visible keypoint are filtered out.
    This strategy seems to make no difference on Detectron and is easier to implement.
    """
    ret = []
    all_num_fg = []
    for proposals_per_image in proposals:
        # If empty/unannotated image (hard negatives), skip filtering for train
        if len(proposals_per_image) == 0:
            ret.append(proposals_per_image)
            continue
        gt_keypoints = proposals_per_image.gt_keypoints.tensor
        # #fg x K x 3
        vis_mask = gt_keypoints[:, :, 2] >= 1
        xs, ys = gt_keypoints[:, :, 0], gt_keypoints[:, :, 1]
        proposal_boxes = proposals_per_image.proposal_boxes.tensor.unsqueeze(dim=1)  # #fg x 1 x 4
        kp_in_box = (
            (xs >= proposal_boxes[:, :, 0])
            & (xs <= proposal_boxes[:, :, 2])
            & (ys >= proposal_boxes[:, :, 1])
            & (ys <= proposal_boxes[:, :, 3])
        )
        selection = (kp_in_box & vis_mask).any(dim=1)
        selection_idxs = torch.nonzero(selection).squeeze(1)
        all_num_fg.append(selection_idxs.numel())
        ret.append(proposals_per_image[selection_idxs])

    storage = get_event_storage()
    storage.put_scalar("keypoint_head/num_fg_samples", np.mean(all_num_fg))
    return ret


class ROIHeads(torch.nn.Module):
    """
    ROIHeads perform all per-region computation in an R-CNN.

    It contains logic of cropping the regions, extract per-region features,
    and make per-region predictions.

    It can have many variants, implemented as subclasses of this class.
    """

    def __init__(self, cfg, input_shape: Dict[str, ShapeSpec]):
        super(ROIHeads, self).__init__()

        # fmt: off
        self.batch_size_per_image     = cfg.MODEL.ROI_HEADS.BATCH_SIZE_PER_IMAGE
        self.positive_sample_fraction = cfg.MODEL.ROI_HEADS.POSITIVE_FRACTION
        self.test_score_thresh        = cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST
        self.test_nms_thresh          = cfg.MODEL.ROI_HEADS.NMS_THRESH_TEST
        self.test_detections_per_img  = cfg.TEST.DETECTIONS_PER_IMAGE
        self.in_features              = cfg.MODEL.ROI_HEADS.IN_FEATURES
        self.num_classes              = cfg.MODEL.ROI_HEADS.NUM_CLASSES
        self.proposal_append_gt       = cfg.MODEL.ROI_HEADS.PROPOSAL_APPEND_GT
        self.feature_strides          = {k: v.stride for k, v in input_shape.items()}
        self.feature_channels         = {k: v.channels for k, v in input_shape.items()}
        self.cls_agnostic_bbox_reg    = cfg.MODEL.ROI_BOX_HEAD.CLS_AGNOSTIC_BBOX_REG
        self.smooth_l1_beta           = cfg.MODEL.ROI_BOX_HEAD.SMOOTH_L1_BETA
        # fmt: on

        # Matcher to assign box proposals to gt boxes
        self.proposal_matcher = Matcher(
            cfg.MODEL.ROI_HEADS.IOU_THRESHOLDS,
            cfg.MODEL.ROI_HEADS.IOU_LABELS,
            allow_low_quality_matches=False,
        )

        # Box2BoxTransform for bounding box regression
        self.box2box_transform = Box2BoxTransform(weights=cfg.MODEL.ROI_BOX_HEAD.BBOX_REG_WEIGHTS)

    def _sample_proposals(self, matched_idxs, matched_labels, gt_classes):
        """
        Based on the matching between N proposals and M groundtruth,
        sample the proposals and set their classification labels.

        Args:
            matched_idxs (Tensor): a vector of length N, each is the best-matched
                gt index in [0, M) for each proposal.
            matched_labels (Tensor): a vector of length N, the matcher's label
                (one of cfg.MODEL.ROI_HEADS.IOU_LABELS) for each proposal.
            gt_classes (Tensor): a vector of length M.

        Returns:
            Tensor: a vector of indices of sampled proposals. Each is in [0, N).
            Tensor: a vector of the same length, the classification label for
                each sampled proposal. Each sample is labeled as either a category in
                [0, num_classes) or the background (num_classes).
        """
        has_gt = gt_classes.numel() > 0
        # Get the corresponding GT for each proposal
        if has_gt:
            gt_classes = gt_classes[matched_idxs]
            # Label unmatched proposals (0 label from matcher) as background (label=num_classes)
            gt_classes[matched_labels == 0] = self.num_classes
            # Label ignore proposals (-1 label)
            gt_classes[matched_labels == -1] = -1
        else:
            gt_classes = torch.zeros_like(matched_idxs) + self.num_classes

        sampled_fg_idxs, sampled_bg_idxs = subsample_labels(
            gt_classes, self.batch_size_per_image, self.positive_sample_fraction, self.num_classes
        )

        sampled_idxs = torch.cat([sampled_fg_idxs, sampled_bg_idxs], dim=0)
        return sampled_idxs, gt_classes[sampled_idxs]

    @torch.no_grad()
    def label_and_sample_proposals(self, proposals, targets):
        """
        Prepare some proposals to be used to train the ROI heads.
        It performs box matching between `proposals` and `targets`, and assigns
        training labels to the proposals.
        It returns ``self.batch_size_per_image`` random samples from proposals and groundtruth
        boxes, with a fraction of positives that is no larger than
        ``self.positive_sample_fraction``.

        Args:
            See :meth:`ROIHeads.forward`

        Returns:
            list[Instances]:
                length `N` list of `Instances`s containing the proposals
                sampled for training. Each `Instances` has the following fields:

                - proposal_boxes: the proposal boxes
                - gt_boxes: the ground-truth box that the proposal is assigned to
                  (this is only meaningful if the proposal has a label > 0; if label = 0
                  then the ground-truth box is random)

                Other fields such as "gt_classes", "gt_masks", that's included in `targets`.
        """
        gt_boxes = [x.gt_boxes for x in targets]
        if self.proposal_append_gt:
            proposals = add_ground_truth_to_proposals(gt_boxes, proposals)

        proposals_with_gt = []

        num_fg_samples = []
        num_bg_samples = []
        for proposals_per_image, targets_per_image in zip(proposals, targets):
            has_gt = len(targets_per_image) > 0
            match_quality_matrix = pairwise_iou(
                targets_per_image.gt_boxes, proposals_per_image.proposal_boxes
            )
            matched_idxs, matched_labels = self.proposal_matcher(match_quality_matrix)
            # －－－－－－－－－－－－－－－－－－－－－－－－－
            iou, _ = match_quality_matrix.max(dim=0)
            # -------------------------------------------------
            sampled_idxs, gt_classes = self._sample_proposals(
                matched_idxs, matched_labels, targets_per_image.gt_classes
            )

            # Set target attributes of the sampled proposals:
            proposals_per_image = proposals_per_image[sampled_idxs]
            proposals_per_image.gt_classes = gt_classes
            # --------------------------------------------
            proposals_per_image.iou = iou[sampled_idxs]
            # --------------------------------------------
        # We index all the attributes of targets that start with "gt_"
            # and have not been added to proposals yet (="gt_classes").
            if has_gt:
                sampled_targets = matched_idxs[sampled_idxs]
                # NOTE: here the indexing waste some compute, because heads
                # like masks, keypoints, etc, will filter the proposals again,
                # (by foreground/background, or number of keypoints in the image, etc)
                # so we essentially index the data twice.
                for (trg_name, trg_value) in targets_per_image.get_fields().items():
                    if trg_name.startswith("gt_") and not proposals_per_image.has(trg_name):
                        proposals_per_image.set(trg_name, trg_value[sampled_targets])
            else:
                gt_boxes = Boxes(
                    targets_per_image.gt_boxes.tensor.new_zeros((len(sampled_idxs), 4))
                )
                proposals_per_image.gt_boxes = gt_boxes

            num_bg_samples.append((gt_classes == self.num_classes).sum().item())
            num_fg_samples.append(gt_classes.numel() - num_bg_samples[-1])
            proposals_with_gt.append(proposals_per_image)

        # Log the number of fg/bg samples that are selected for training ROI heads
        storage = get_event_storage()
        storage.put_scalar("roi_head/num_fg_samples", np.mean(num_fg_samples))
        storage.put_scalar("roi_head/num_bg_samples", np.mean(num_bg_samples))

        return proposals_with_gt

    def forward(self, images, features, proposals, targets=None):
        """
        Args:
            images (ImageList):
            features (dict[str: Tensor]): input data as a mapping from feature
                map name to tensor. Axis 0 represents the number of images `N` in
                the input data; axes 1-3 are channels, height, and width, which may
                vary between feature maps (e.g., if a feature pyramid is used).
            proposals (list[Instances]): length `N` list of `Instances`s. The i-th
                `Instances` contains object proposals for the i-th input image,
                with fields "proposal_boxes" and "objectness_logits".
            targets (list[Instances], optional): length `N` list of `Instances`s. The i-th
                `Instances` contains the ground-truth per-instance annotations
                for the i-th input image.  Specify `targets` during training only.
                It may have the following fields:

                - gt_boxes: the bounding box of each instance.
                - gt_classes: the label for each instance with a category ranging in [0, #class].
                - gt_masks: PolygonMasks or BitMasks, the ground-truth masks of each instance.
                - gt_keypoints: NxKx3, the groud-truth keypoints for each instance.

        Returns:
            results (list[Instances]): length `N` list of `Instances`s containing the
            detected instances. Returned during inference only; may be [] during training.

            losses (dict[str->Tensor]):
            mapping from a named loss to a tensor storing the loss. Used during training only.
        """
        raise NotImplementedError()


@ROI_HEADS_REGISTRY.register()
class Res5ROIHeads(ROIHeads):
    """
    The ROIHeads in a typical "C4" R-CNN model, where
    the box and mask head share the cropping and
    the per-region feature computation by a Res5 block.
    """

    def __init__(self, cfg, input_shape):
        super().__init__(cfg, input_shape)

        assert len(self.in_features) == 1

        # fmt: off
        pooler_resolution = cfg.MODEL.ROI_BOX_HEAD.POOLER_RESOLUTION
        pooler_type       = cfg.MODEL.ROI_BOX_HEAD.POOLER_TYPE
        pooler_scales     = (1.0 / self.feature_strides[self.in_features[0]], )
        sampling_ratio    = cfg.MODEL.ROI_BOX_HEAD.POOLER_SAMPLING_RATIO
        self.mask_on      = cfg.MODEL.MASK_ON
        # fmt: on
        assert not cfg.MODEL.KEYPOINT_ON

        self.pooler = ROIPooler(
            output_size=pooler_resolution,
            scales=pooler_scales,
            sampling_ratio=sampling_ratio,
            pooler_type=pooler_type,
        )

        self.res5, out_channels = self._build_res5_block(cfg)
        self.box_predictor = build_predictor(cfg, out_channels)

        if self.mask_on:
            self.mask_head = build_mask_head(
                cfg,
                ShapeSpec(channels=out_channels, width=pooler_resolution, height=pooler_resolution),
            )

    def _build_res5_block(self, cfg):
        # fmt: off
        stage_channel_factor = 2 ** 3  # res5 is 8x res2
        num_groups           = cfg.MODEL.RESNETS.NUM_GROUPS
        width_per_group      = cfg.MODEL.RESNETS.WIDTH_PER_GROUP
        bottleneck_channels  = num_groups * width_per_group * stage_channel_factor
        out_channels         = cfg.MODEL.RESNETS.RES2_OUT_CHANNELS * stage_channel_factor
        stride_in_1x1        = cfg.MODEL.RESNETS.STRIDE_IN_1X1
        norm                 = cfg.MODEL.RESNETS.NORM
        assert not cfg.MODEL.RESNETS.DEFORM_ON_PER_STAGE[-1], \
            "Deformable conv is not yet supported in res5 head."
        # fmt: on

        blocks = make_stage(
            BottleneckBlock,
            3,
            first_stride=2,
            in_channels=out_channels // 2,
            bottleneck_channels=bottleneck_channels,
            out_channels=out_channels,
            num_groups=num_groups,
            norm=norm,
            stride_in_1x1=stride_in_1x1,
        )
        return nn.Sequential(*blocks), out_channels

    def _shared_roi_transform(self, features, boxes):
        x = self.pooler(features, boxes)
        return self.res5(x)

    def forward(self, images, features, proposals, targets=None):
        """
        See :class:`ROIHeads.forward`.
        """
        del images

        if self.training:
            proposals = self.label_and_sample_proposals(proposals, targets)

        if self.training:
            del targets
            losses, box_features = self._forward_box(features, proposals)
            if self.mask_on:
                proposals, fg_selection_masks = select_foreground_proposals(
                    proposals, self.num_classes
                )
                # Since the ROI feature transform is shared between boxes and masks,
                # we don't need to recompute features. The mask loss is only defined
                # on foreground proposals, so we need to select out the foreground
                # features.
                mask_features = box_features[torch.cat(fg_selection_masks, dim=0)]
                del box_features
                mask_logits = self.mask_head(mask_features)
                losses["loss_mask"] = mask_rcnn_loss(mask_logits, proposals)
            return [], losses
        else:
            if targets is not None:  # for cls_score parameters initialization
                return self._init_weight([features[f] for f in self.in_features], targets)
            del targets
            # proposals[0] = proposals[0][:512]  # for calculating the training FLOPs
            pred_instances = self._forward_box(features, proposals)
            pred_instances = self.forward_with_given_boxes(features, pred_instances)
            return pred_instances, {}

    def _forward_box(self, features, proposals):
        """
        Forward logic of the box prediction branch.

        Args:
            features (list[Tensor]): #level input features for box prediction
            proposals (list[Instances]): the per-image object proposals with
                their matching ground truth.
                Each has fields "proposal_boxes", and "objectness_logits",
                "gt_classes", "gt_boxes".

        Returns:
            In training, a dict of losses.
            In inference, a list of `Instances`, the predicted instances.
        """
        proposal_boxes = [x.proposal_boxes for x in proposals]
        box_features = self._shared_roi_transform(
            [features[f] for f in self.in_features], proposal_boxes
        )
        feature_pooled = box_features.mean(dim=[2, 3])  # pooled to 1x1
        pred_class_logits, pred_proposal_deltas = self.box_predictor(feature_pooled)
        del feature_pooled

        outputs = FastRCNNOutputs(
            self.box2box_transform,
            pred_class_logits,
            pred_proposal_deltas,
            proposals,
            self.smooth_l1_beta,
        )

        if self.training:
            return outputs.losses(), box_features
        else:
            pred_instances, _ = outputs.inference(
                self.test_score_thresh, self.test_nms_thresh, self.test_detections_per_img
            )
            return pred_instances

    def _init_weight(self, features, targets):
        box_features = self._shared_roi_transform(features, [x.gt_boxes for x in targets])
        activations = box_features.mean(dim=[2, 3])
        gt_classes = torch.cat([x.gt_classes for x in targets], dim=0)
        keep = gt_classes != -1
        activations = activations[keep]
        gt_classes = gt_classes[keep].tolist()
        cls_dict_act = {i: [] for i in range(self.num_classes)}
        for i in range(len(gt_classes)):
            cls_dict_act[gt_classes[i]].append(activations[i])
        del targets
        return cls_dict_act

    def forward_with_given_boxes(self, features, instances):
        """
        Use the given boxes in `instances` to produce other (non-box) per-ROI outputs.

        Args:
            features: same as in `forward()`
            instances (list[Instances]): instances to predict other outputs. Expect the keys
                "pred_boxes" and "pred_classes" to exist.

        Returns:
            instances (Instances):
                the same `Instances` object, with extra
                fields such as `pred_masks` or `pred_keypoints`.
        """
        assert not self.training
        assert instances[0].has("pred_boxes") and instances[0].has("pred_classes")

        if self.mask_on:
            features = [features[f] for f in self.in_features]
            x = self._shared_roi_transform(features, [x.pred_boxes for x in instances])
            mask_logits = self.mask_head(x)
            mask_rcnn_inference(mask_logits, instances)
        return instances


@ROI_HEADS_REGISTRY.register()
class StandardROIHeads(ROIHeads):
    """
    It's "standard" in a sense that there is no ROI transform sharing
    or feature sharing between tasks.
    The cropped rois go to separate branches directly.
    This way, it is easier to make separate abstractions for different branches.

    This class is used by most models, such as FPN and C5.
    To implement more models, you can subclass it and implement a different
    :meth:`forward()` or a head.
    """

    def __init__(self, cfg, input_shape):
        super(StandardROIHeads, self).__init__(cfg, input_shape)
        self._init_box_head(cfg)

    def _init_box_head(self, cfg):
        # fmt: off
        pooler_resolution = cfg.MODEL.ROI_BOX_HEAD.POOLER_RESOLUTION
        pooler_scales     = tuple(1.0 / self.feature_strides[k] for k in self.in_features)
        sampling_ratio    = cfg.MODEL.ROI_BOX_HEAD.POOLER_SAMPLING_RATIO
        pooler_type       = cfg.MODEL.ROI_BOX_HEAD.POOLER_TYPE
        # fmt: on

        # If StandardROIHeads is applied on multiple feature maps (as in FPN),
        # then we share the same predictors and therefore the channel counts must be the same
        in_channels = [self.feature_channels[f] for f in self.in_features]
        # Check all channel counts are equal
        assert len(set(in_channels)) == 1, in_channels
        in_channels = in_channels[0]
        # ROI_pooling
        self.box_pooler = ROIPooler(
            output_size=pooler_resolution,
            scales=pooler_scales,
            sampling_ratio=sampling_ratio,
            pooler_type=pooler_type,
        )
        # Here we split "box head" and "box predictor", which is mainly due to historical reasons.
        # They are used together so the "box predictor" layers should be part of the "box head".
        # New subclasses of ROIHeads do not need "box predictor"s.
        # box_head, FastRCNNConvFCHead, 2FC+relu,
        self.box_head = build_box_head(
            cfg, ShapeSpec(channels=in_channels, height=pooler_resolution, width=pooler_resolution)
        )
        # CosineSimOutputLayer
        # self.output_layer_name = cfg.MODEL.ROI_HEADS.OUTPUT_LAYER
        self.box_predictor =build_predictor(cfg, self.box_head.output_size)

    def forward(self, images, features, proposals, targets=None):
        """
        See :class:`ROIHeads.forward`.
            proposals (List[Instance]): fields=[proposal_boxes, objectness_logits]
                post_nms_top_k proposals for each image， len = N

            targets (List[Instance]):   fields=[gt_boxes, gt_classes]
                gt_instances for each image, len = N
        """
        del images
        features_list = [features[f] for f in self.in_features]
        if self.training:
            # label and sample 256 from post_nms_top_k each images
            # has field [proposal_boxes, objectness_logits ,gt_classes, gt_boxes]
            proposals = self.label_and_sample_proposals(proposals, targets)
        # del targets

        if self.training:
            # FastRCNNOutputs.losses()
            # {'loss_cls':, 'loss_box_reg':}
            del targets
            losses = self._forward_box(features_list, proposals)  # get losses from fast_rcnn.py::FastRCNNOutputs
            return proposals, losses  # return to rcnn.py line 201
        else:
            if targets is not None:  # for cls_score parameters initialization
                return self._init_weight(features_list, targets)
            del targets
            # proposals[0] = proposals[0][:512]  # for calculating the training FLOPs
            pred_instances = self._forward_box(features_list, proposals)
            # During inference cascaded prediction is used: the mask and keypoints heads are only
            # applied to the top scoring box detections.
            # pred_instances = self.forward_with_given_boxes(features, pred_instances)
            return pred_instances, {}

    def forward_with_given_boxes(self, features, instances):
            """
            Use the given boxes in `instances` to produce other (non-box) per-ROI outputs.

            This is useful for downstream tasks where a box is known, but need to obtain
            other attributes (outputs of other heads).
            Test-time augmentation also uses this.

            Args:
                features: same as in `forward()`
                instances (list[Instances]): instances to predict other outputs. Expect the keys
                    "pred_boxes" and "pred_classes" to exist.

            Returns:
                instances (Instances):
                    the same `Instances` object, with extra
                    fields such as `pred_masks` or `pred_keypoints`.
            """
            assert not self.training
            assert instances[0].has("pred_boxes") and instances[0].has("pred_classes")
            features = [features[f] for f in self.in_features]

            instances = self._forward_mask(features, instances)
            instances = self._forward_keypoint(features, instances)
            return instances

    def _init_weight(self, features, targets):
        box_features = self.box_pooler(features, [x.gt_boxes for x in targets])
        activations =  self.box_head(box_features)
        # activations = self.meta_head(box_features) if self.meta_on else self.box_head(box_features)
        gt_classes = torch.cat([x.gt_classes for x in targets], dim=0)
        keep = gt_classes != -1
        activations = activations[keep]
        gt_classes = gt_classes[keep].tolist()
        cls_dict_act = {i: [] for i in range(self.num_classes)}
        for i in range(len(gt_classes)):
            cls_dict_act[gt_classes[i]].append(activations[i])
        del targets
        return cls_dict_act

    def _forward_box(self, features, proposals):
        """
        Forward logic of the box prediction branch.

        Args:
            features (list[Tensor]): #level input features for box prediction
            proposals (list[Instances]): the per-image object proposals with
                their matching ground truth.
                Each has fields "proposal_boxes", and "objectness_logits",
                "gt_classes", "gt_boxes".

        Returns:
            In training, a dict of losses.
            In inference, a list of `Instances`, the predicted instances.
        """
        box_features = self.box_pooler(features, [x.proposal_boxes for x in proposals])  # [None, 256, POOLER_RESOLU, POOLER_RESOLU]
        box_features = self.box_head(box_features)  # [None, FC_DIM]
        pred_class_logits, pred_proposal_deltas = self.box_predictor(box_features)
        del box_features

        outputs = FastRCNNOutputs(
            self.box2box_transform,
            pred_class_logits,
            pred_proposal_deltas,
            proposals,
            self.smooth_l1_beta,
        )
        if self.training:
            return outputs.losses()
        else:
            pred_instances, _ = outputs.inference(
                self.test_score_thresh, self.test_nms_thresh, self.test_detections_per_img
            )
            return pred_instances


# class StandardROIHeads(ROIHeads):
#     """
#     It's "standard" in a sense that there is no ROI transform sharing
#     or feature sharing between tasks.
#     The cropped rois go to separate branches (boxes and masks) directly.
#     This way, it is easier to make separate abstractions for different branches.
#
#     This class is used by most models, such as FPN and C5.
#     To implement more models, you can subclass it and implement a different
#     :meth:`forward()` or a head.
#     """
#
#     def __init__(self, cfg, input_shape):
#         super(StandardROIHeads, self).__init__(cfg, input_shape)
#         self._init_meta_head(cfg)
#         self._init_box_head(cfg)
#         self._init_mask_head(cfg)
#         self._init_keypoint_head(cfg)
#
#     def _init_meta_head(self, cfg):
#         # fmt: off
#         self.meta_on = cfg.MODEL.META_ON
#         if not self.meta_on:
#             return
#         # Number of images per GPU that will be used to predict meta weight.
#         self.ims_per_gpu = cfg.MODEL.ROI_META_HEAD.IMS_PER_GPU
#         self.momentum = cfg.MODEL.ROI_META_HEAD.MOMENTUM
#
#         pooler_resolution = cfg.MODEL.ROI_META_HEAD.POOLER_RESOLUTION
#         pooler_scales = tuple(1.0 / self.feature_strides[k] for k in self.in_features)
#         sampling_ratio = cfg.MODEL.ROI_META_HEAD.POOLER_SAMPLING_RATIO
#         pooler_type = cfg.MODEL.ROI_META_HEAD.POOLER_TYPE
#         # fmt: on
#
#         # If StandardROIHeads is applied on multiple feature maps (as in FPN),
#         # then we share the same predictors and therefore the channel counts must be the same
#         in_channels = [self.feature_channels[f] for f in self.in_features]
#         # Check all channel counts are equal
#         assert len(set(in_channels)) == 1, in_channels
#         in_channels = in_channels[0]
#
#         self.meta_pooler = ROIPooler(
#             output_size=pooler_resolution,
#             scales=pooler_scales,
#             sampling_ratio=sampling_ratio,
#             pooler_type=pooler_type,
#         )
#         self.meta_head = build_meta_head(
#             cfg, ShapeSpec(channels=in_channels, height=pooler_resolution, width=pooler_resolution)
#         )
#
#     def _init_box_head(self, cfg):
#         # fmt: off
#         pooler_resolution = cfg.MODEL.ROI_BOX_HEAD.POOLER_RESOLUTION
#         pooler_scales     = tuple(1.0 / self.feature_strides[k] for k in self.in_features)
#         sampling_ratio    = cfg.MODEL.ROI_BOX_HEAD.POOLER_SAMPLING_RATIO
#         pooler_type       = cfg.MODEL.ROI_BOX_HEAD.POOLER_TYPE
#         # fmt: on
#
#         # If StandardROIHeads is applied on multiple feature maps (as in FPN),
#         # then we share the same predictors and therefore the channel counts must be the same
#         in_channels = [self.feature_channels[f] for f in self.in_features]
#         # Check all channel counts are equal
#         assert len(set(in_channels)) == 1, in_channels
#         in_channels = in_channels[0]
#
#         self.box_pooler = ROIPooler(
#             output_size=pooler_resolution,
#             scales=pooler_scales,
#             sampling_ratio=sampling_ratio,
#             pooler_type=pooler_type,
#         )
#         # Here we split "box head" and "box predictor", which is mainly due to historical reasons.
#         # They are used together so the "box predictor" layers should be part of the "box head".
#         # New subclasses of ROIHeads do not need "box predictor"s.
#         self.box_head = build_box_head(
#             cfg, ShapeSpec(channels=in_channels, height=pooler_resolution, width=pooler_resolution)
#         )
#         self.box_predictor = build_predictor(cfg, self.box_head.output_size)
#
#     def _init_mask_head(self, cfg):
#         # fmt: off
#         self.mask_on           = cfg.MODEL.MASK_ON
#         if not self.mask_on:
#             return
#         pooler_resolution = cfg.MODEL.ROI_MASK_HEAD.POOLER_RESOLUTION
#         pooler_scales     = tuple(1.0 / self.feature_strides[k] for k in self.in_features)
#         sampling_ratio    = cfg.MODEL.ROI_MASK_HEAD.POOLER_SAMPLING_RATIO
#         pooler_type       = cfg.MODEL.ROI_MASK_HEAD.POOLER_TYPE
#         # fmt: on
#
#         in_channels = [self.feature_channels[f] for f in self.in_features][0]
#
#         self.mask_pooler = ROIPooler(
#             output_size=pooler_resolution,
#             scales=pooler_scales,
#             sampling_ratio=sampling_ratio,
#             pooler_type=pooler_type,
#         )
#         self.mask_head = build_mask_head(
#             cfg, ShapeSpec(channels=in_channels, width=pooler_resolution, height=pooler_resolution)
#         )
#
#     def _init_keypoint_head(self, cfg):
#         # fmt: off
#         self.keypoint_on                         = cfg.MODEL.KEYPOINT_ON
#         if not self.keypoint_on:
#             return
#         pooler_resolution                        = cfg.MODEL.ROI_KEYPOINT_HEAD.POOLER_RESOLUTION
#         pooler_scales                            = tuple(1.0 / self.feature_strides[k] for k in self.in_features)  # noqa
#         sampling_ratio                           = cfg.MODEL.ROI_KEYPOINT_HEAD.POOLER_SAMPLING_RATIO
#         pooler_type                              = cfg.MODEL.ROI_KEYPOINT_HEAD.POOLER_TYPE
#         self.normalize_loss_by_visible_keypoints = cfg.MODEL.ROI_KEYPOINT_HEAD.NORMALIZE_LOSS_BY_VISIBLE_KEYPOINTS  # noqa
#         self.keypoint_loss_weight                = cfg.MODEL.ROI_KEYPOINT_HEAD.LOSS_WEIGHT
#         # fmt: on
#
#         in_channels = [self.feature_channels[f] for f in self.in_features][0]
#
#         self.keypoint_pooler = ROIPooler(
#             output_size=pooler_resolution,
#             scales=pooler_scales,
#             sampling_ratio=sampling_ratio,
#             pooler_type=pooler_type,
#         )
#         self.keypoint_head = build_keypoint_head(
#             cfg, ShapeSpec(channels=in_channels, width=pooler_resolution, height=pooler_resolution)
#         )
#
#     def forward(self, images, features, proposals, targets=None):
#         """
#         See :class:`ROIHeads.forward`.
#         """
#         del images
#         features_list = [features[f] for f in self.in_features]
#         if self.training:
#             proposals = self.label_and_sample_proposals(proposals, targets)
#
#         if self.training:
#             if self.meta_on:
#                 self._forward_meta(features_list, targets)
#                 features_list = [x[self.ims_per_gpu:] for x in features_list]
#                 proposals = proposals[self.ims_per_gpu:]
#             del targets
#             losses = self._forward_box(features_list, proposals)
#             # During training the proposals used by the box head are
#             # used by the mask, keypoint (and densepose) heads.
#             losses.update(self._forward_mask(features_list, proposals))
#             losses.update(self._forward_keypoint(features_list, proposals))
#             return proposals, losses
#         else:
#             if targets is not None:  # for cls_score parameters initialization
#                 return self._init_weight(features_list, targets)
#             del targets
#             # proposals[0] = proposals[0][:512]  # for calculating the training FLOPs
#             pred_instances = self._forward_box(features_list, proposals)
#             # During inference cascaded prediction is used: the mask and keypoints heads are only
#             # applied to the top scoring box detections.
#             pred_instances = self.forward_with_given_boxes(features, pred_instances)
#             return pred_instances, {}
#
#     def _init_weight(self, features, targets):
#         box_features = self.box_pooler(features, [x.gt_boxes for x in targets])
#         activations = self.meta_head(box_features) if self.meta_on else self.box_head(box_features)
#         gt_classes = torch.cat([x.gt_classes for x in targets], dim=0)
#         keep = gt_classes != -1
#         activations = activations[keep]
#         gt_classes = gt_classes[keep].tolist()
#         cls_dict_act = {i: [] for i in range(self.num_classes)}
#         for i in range(len(gt_classes)):
#             cls_dict_act[gt_classes[i]].append(activations[i])
#         del targets
#         return cls_dict_act
#
#     def forward_with_given_boxes(self, features, instances):
#         """
#         Use the given boxes in `instances` to produce other (non-box) per-ROI outputs.
#
#         This is useful for downstream tasks where a box is known, but need to obtain
#         other attributes (outputs of other heads).
#         Test-time augmentation also uses this.
#
#         Args:
#             features: same as in `forward()`
#             instances (list[Instances]): instances to predict other outputs. Expect the keys
#                 "pred_boxes" and "pred_classes" to exist.
#
#         Returns:
#             instances (Instances):
#                 the same `Instances` object, with extra
#                 fields such as `pred_masks` or `pred_keypoints`.
#         """
#         assert not self.training
#         assert instances[0].has("pred_boxes") and instances[0].has("pred_classes")
#         features = [features[f] for f in self.in_features]
#
#         instances = self._forward_mask(features, instances)
#         instances = self._forward_keypoint(features, instances)
#         return instances
#
#     def _forward_meta(self, features, targets):
#         """
#         Forward logic of the weight prediction branch.
#
#         Args:
#             features (list[Tensor]): #level input features for weight prediction
#             targets (list[Instances]): the per-image object ground truth.
#                 Each has fields "gt_classes", "gt_boxes".
#         """
#         meta_features = self.meta_pooler([x[:self.ims_per_gpu if self.training else None] for x in features],
#                                          [x.gt_boxes for x in targets[:self.ims_per_gpu if self.training else None]])
#         local_meta_weight = self.meta_head(meta_features)
#         local_gt_classes = torch.cat([x.gt_classes for x in
#                                       targets[:self.ims_per_gpu if self.training else None]], dim=0)
#         meta_weight, global_gt_classes = self._gather(local_meta_weight, local_gt_classes)
#
#         # In each iteration, the meta branch only predicts a subset of class weights, therefore
#         # filtering out irrelevant class weights that should not be multiplied by momentum
#         gt_mask = global_gt_classes.unique()
#         momentum = meta_weight.new_ones((self.num_classes, 1)).index_fill_(0, gt_mask, self.momentum)
#         if self.training:
#             self.box_predictor.new_weight = self.box_predictor.cls_score.weight.clone()
#             self.box_predictor.new_weight[:-1] = momentum * self.box_predictor.new_weight[:-1] + (1. - momentum) * meta_weight
#             self.box_predictor.cls_score.weight.data = self.box_predictor.new_weight
#         else:
#             self.box_predictor.cls_score.weight.data[:-1] = momentum * self.box_predictor.cls_score.weight.data[:-1] + \
#                                                             (1. - momentum) * meta_weight
#
#     def _gather(self, local_features, local_gt_classes):
#         """
#         This function performs two operations. 1) gather the "local_features" across
#         different GPUs to obtain the "global_features". The same is applied to the
#         "local_gt_classes" to yield "global_gt_classes". 2) aggregate the global features
#         into class-wise representations according to their class labels by norm-mean
#         (norm followed by mean).
#
#         Args:
#             local_features (Tensor): Instance features to be gathered, of shape (num, feature_dim).
#             local_gt_classes (Tensor): Corresponding class label for "local_features", of shape (num,).
#
#         Returns:
#             cls_feat (Tensor): class-wise features gathered and aggregated from local_features.
#             global_gt_classes (Tensor): class labels gathered from local_gt_class.
#         """
#         # filter out ignored classes
#         keep = local_gt_classes != -1
#         local_features = local_features[keep]
#         local_gt_classes = local_gt_classes[keep]
#
#         # gather features across different GPU devices
#         feat_list = comm.all_gather_grad(local_features)
#         gt_cls_list = comm.all_gather_grad(local_gt_classes)
#         global_features = torch.cat(feat_list, dim=0)
#         global_gt_classes = torch.cat(gt_cls_list, dim=0)
#
#         cls_list = [torch.empty(0, device='cuda') for _ in range(self.num_classes)]
#         # ensure the variables on the same device
#         assert all([x.device == global_features.device for x in cls_list]), \
#             'Variable global_features and cls_list should be on the same device.'
#         for i, gt_cls in enumerate(global_gt_classes.tolist()):
#             cls_list[gt_cls] = torch.cat((cls_list[gt_cls], global_features[i][None, :]), dim=0)
#         cls_list = [F.normalize(x, dim=1).mean(0) if torch.numel(x)
#                       else torch.zeros(global_features.size(1), device='cuda') for x in cls_list]
#         cls_feat = torch.stack(cls_list, dim=0)
#         return cls_feat, global_gt_classes
#
#     def _forward_box(self, features, proposals):
#         """
#         Forward logic of the box prediction branch.
#
#         Args:
#             features (list[Tensor]): #level input features for box prediction
#             proposals (list[Instances]): the per-image object proposals with
#                 their matching ground truth.
#                 Each has fields "proposal_boxes", and "objectness_logits",
#                 "gt_classes", "gt_boxes".
#
#         Returns:
#             In training, a dict of losses.
#             In inference, a list of `Instances`, the predicted instances.
#         """
#         box_features = self.box_pooler(features, [x.proposal_boxes for x in proposals])
#         box_features = self.box_head(box_features)
#         pred_class_logits, pred_proposal_deltas = self.box_predictor(box_features)
#         del box_features
#
#         outputs = FastRCNNOutputs(
#             self.box2box_transform,
#             pred_class_logits,
#             pred_proposal_deltas,
#             proposals,
#             self.smooth_l1_beta,
#         )
#         if self.training:
#             return outputs.losses()
#         else:
#             pred_instances, _ = outputs.inference(
#                 self.test_score_thresh, self.test_nms_thresh, self.test_detections_per_img
#             )
#             return pred_instances
#
#     def _forward_mask(self, features, instances):
#         """
#         Forward logic of the mask prediction branch.
#
#         Args:
#             features (list[Tensor]): #level input features for mask prediction
#             instances (list[Instances]): the per-image instances to train/predict masks.
#                 In training, they can be the proposals.
#                 In inference, they can be the predicted boxes.
#
#         Returns:
#             In training, a dict of losses.
#             In inference, update `instances` with new fields "pred_masks" and return it.
#         """
#         if not self.mask_on:
#             return {} if self.training else instances
#
#         if self.training:
#             # The loss is only defined on positive proposals.
#             proposals, _ = select_foreground_proposals(instances, self.num_classes)
#             proposal_boxes = [x.proposal_boxes for x in proposals]
#             mask_features = self.mask_pooler(features, proposal_boxes)
#             mask_logits = self.mask_head(mask_features)
#             return {"loss_mask": mask_rcnn_loss(mask_logits, proposals)}
#         else:
#             pred_boxes = [x.pred_boxes for x in instances]
#             mask_features = self.mask_pooler(features, pred_boxes)
#             mask_logits = self.mask_head(mask_features)
#             mask_rcnn_inference(mask_logits, instances)
#             return instances
#
#     def _forward_keypoint(self, features, instances):
#         """
#         Forward logic of the keypoint prediction branch.
#
#         Args:
#             features (list[Tensor]): #level input features for keypoint prediction
#             instances (list[Instances]): the per-image instances to train/predict keypoints.
#                 In training, they can be the proposals.
#                 In inference, they can be the predicted boxes.
#
#         Returns:
#             In training, a dict of losses.
#             In inference, update `instances` with new fields "pred_keypoints" and return it.
#         """
#         if not self.keypoint_on:
#             return {} if self.training else instances
#
#         num_images = len(instances)
#
#         if self.training:
#             # The loss is defined on positive proposals with at >=1 visible keypoints.
#             proposals, _ = select_foreground_proposals(instances, self.num_classes)
#             proposals = select_proposals_with_visible_keypoints(proposals)
#             proposal_boxes = [x.proposal_boxes for x in proposals]
#
#             keypoint_features = self.keypoint_pooler(features, proposal_boxes)
#             keypoint_logits = self.keypoint_head(keypoint_features)
#
#             normalizer = (
#                 num_images
#                 * self.batch_size_per_image
#                 * self.positive_sample_fraction
#                 * keypoint_logits.shape[1]
#             )
#             loss = keypoint_rcnn_loss(
#                 keypoint_logits,
#                 proposals,
#                 normalizer=None if self.normalize_loss_by_visible_keypoints else normalizer,
#             )
#             return {"loss_keypoint": loss * self.keypoint_loss_weight}
#         else:
#             pred_boxes = [x.pred_boxes for x in instances]
#             keypoint_features = self.keypoint_pooler(features, pred_boxes)
#             keypoint_logits = self.keypoint_head(keypoint_features)
#             keypoint_rcnn_inference(keypoint_logits, instances)
#             return instances


@ROI_HEADS_REGISTRY.register()
class ReweightedROIHeads(StandardROIHeads):
    def __init__(self, cfg, input_shape):
        super(ReweightedROIHeads, self).__init__(cfg, input_shape)
        self.setting = cfg.SETTING
        self._init_reweight_layer()

    def _init_box_head(self, cfg):
        # fmt: off
        pooler_resolution = cfg.MODEL.ROI_BOX_HEAD.POOLER_RESOLUTION
        pooler_scales     = tuple(1.0 / self.feature_strides[k] for k in self.in_features)
        sampling_ratio    = cfg.MODEL.ROI_BOX_HEAD.POOLER_SAMPLING_RATIO
        pooler_type       = cfg.MODEL.ROI_BOX_HEAD.POOLER_TYPE
        # fmt: on

        # If StandardROIHeads is applied on multiple feature maps (as in FPN),
        # then we share the same predictors and therefore the channel counts must be the same
        in_channels = [self.feature_channels[f] for f in self.in_features]
        # Check all channel counts are equal
        assert len(set(in_channels)) == 1, in_channels
        in_channels = in_channels[0]

        self.box_pooler = ROIPooler(
            output_size=pooler_resolution,
            scales=pooler_scales,
            sampling_ratio=sampling_ratio,
            pooler_type=pooler_type,
        )
        # Here we split "box head" and "box predictor", which is mainly due to historical reasons.
        # They are used together so the "box predictor" layers should be part of the "box head".
        # New subclasses of ROIHeads do not need "box predictor"s.
        self.box_head = build_box_head(
            cfg, ShapeSpec(channels=in_channels, height=pooler_resolution, width=pooler_resolution)
        )
        # if cfg.SETTING == 'Incremental':
        #     self.box_head_novel = build_box_head(
        #         cfg, ShapeSpec(channels=in_channels, height=pooler_resolution, width=pooler_resolution)
        #     )
        self.box_predictor = build_predictor(cfg, self.box_head.output_size)

    def _init_reweight_layer(self):
        # If StandardROIHeads is applied on multiple feature maps (as in FPN),
        # then we share the same predictors and therefore the channel counts must be the same
        in_channels = [self.feature_channels[f] for f in self.in_features]
        # Check all channel counts are equal
        assert len(set(in_channels)) == 1, in_channels
        in_channels = in_channels[0]
        if self.setting == 'Transfer':
            self.reweight = torch.nn.Linear(in_channels, self.num_classes, bias=False)
        elif self.setting == 'Incremental':
            self.reweight = torch.nn.Linear(in_channels, 6, bias=False)
        else:
            raise ValueError("Unsupported setting: {}".format(self.setting))
        nn.init.kaiming_normal_(self.reweight.weight, mode="fan_out", nonlinearity="relu")

    def forward(self, images, features, proposals, targets=None):
        """
        See :class:`ROIHeads.forward`.
        """
        del images
        features_list = [features[f] for f in self.in_features]
        if self.training:
            proposals = self.label_and_sample_proposals(proposals, targets)
        else:
            if targets is not None:  # for reweight parameters initialization
                return self._init_reweight(features_list, targets)
        del targets

        if self.training:
            losses = self._forward_box(features_list, proposals)
            # During training the proposals used by the box head are
            # used by the mask, keypoint (and densepose) heads.
            losses.update(self._forward_mask(features_list, proposals))
            losses.update(self._forward_keypoint(features_list, proposals))
            return proposals, losses
        else:
            pred_instances = self._forward_box(features_list, proposals)
            # During inference cascaded prediction is used: the mask and keypoints heads are only
            # applied to the top scoring box detections.
            pred_instances = self.forward_with_given_boxes(features, pred_instances)
            return pred_instances, {}

    def _init_reweight(self, features, targets):
        box_features = self.box_pooler(features, [x.gt_boxes for x in targets])
        activations = self.box_head(box_features)
        box_features = nn.functional.avg_pool2d(box_features, self.box_pooler.output_size).squeeze()
        gt_classes = torch.cat([x.gt_classes for x in targets], dim=0)
        keep = gt_classes != -1
        box_features = box_features[keep]
        activations = activations[keep]
        gt_classes = gt_classes[keep].tolist()
        cls_dict_feat = {i: [] for i in range(self.num_classes)}
        cls_dict_act = {i: [] for i in range(self.num_classes)}
        for i in range(len(gt_classes)):
            cls_dict_feat[gt_classes[i]].append(box_features[i])
            cls_dict_act[gt_classes[i]].append(activations[i])
        return cls_dict_feat, cls_dict_act

    def _forward_box(self, features, proposals):
        """
        Forward logic of the box prediction branch.

        Args:
            features (list[Tensor]): #level input features for box prediction
            proposals (list[Instances]): the per-image object proposals with
                their matching ground truth.
                Each has fields "proposal_boxes", and "objectness_logits",
                "gt_classes", "gt_boxes".

        Returns:
            In training, a dict of losses.
            In inference, a list of `Instances`, the predicted instances.
        """
        box_features = self.box_pooler(features, [x.proposal_boxes for x in proposals])  # [2*512, 256, 7 ,7]

        assert self.reweight.weight.dim() == 2, 'The dim of reweight parameters should be 2.'
        if self.setting == 'Transfer':
            weight = torch.cat([self.reweight.weight,
                                torch.ones((1, self.reweight.weight.size(1)),
                                           device=self.reweight.weight.device)], dim=0
                               )
            box_features = weight[:, :, None, None] * box_features.unsqueeze(1)
            box_features = self.box_head(box_features)
        elif self.setting == 'Incremental':
            box_features = self.reweight.weight[:, :, None, None] * box_features.unsqueeze(1)
            box_features = self.box_head(box_features)
            # base_box_features = box_features[:, 0].unsqueeze(1)
            # novel_box_features = box_features[:, 1:]
            # base_box_features = self.box_head(base_box_features)
            # novel_box_features = self.box_head_novel(novel_box_features)
            # box_features = torch.cat((base_box_features, novel_box_features), dim=1)
        else:
            raise ValueError("Unsupported setting: {}".format(self.setting))
        pred_class_logits, pred_proposal_deltas = self.box_predictor(box_features)
        del box_features

        # objectness_logits = torch.cat([p.objectness_logits for p in proposals], dim=0)
        # bg_logits = torch.log(torch.exp(pred_class_logits).sum(dim=1, keepdim=True))
        # fg_logits = objectness_logits[:, None] + pred_class_logits
        # pred_class_logits = torch.cat([fg_logits, bg_logits], dim=1)

        outputs = FastRCNNOutputs(
            self.box2box_transform,
            pred_class_logits,
            pred_proposal_deltas,
            proposals,
            self.smooth_l1_beta,
        )

        if self.training:
            return outputs.losses()
        else:
            pred_instances, _ = outputs.inference(
                self.test_score_thresh, self.test_nms_thresh, self.test_detections_per_img
            )
            return pred_instances


@ROI_HEADS_REGISTRY.register()
class ContrastiveROIHeads(StandardROIHeads):
    def __init__(self, cfg, input_shape):
        super().__init__(cfg, input_shape)
        # fmt: on
        self.fc_dim               = cfg.MODEL.ROI_BOX_HEAD.FC_DIM
        self.mlp_head_dim         = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.MLP_FEATURE_DIM
        self.temperature          = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.TEMPERATURE
        self.contrast_loss_weight = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.LOSS_WEIGHT #1
        self.box_reg_weight       = cfg.MODEL.ROI_BOX_HEAD.BOX_REG_WEIGHT
        self.weight_decay         = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.DECAY.ENABLED
        self.decay_steps          = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.DECAY.STEPS
        self.decay_rate           = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.DECAY.RATE

        self.num_classes          = cfg.MODEL.ROI_HEADS.NUM_CLASSES

        self.loss_version         = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.LOSS_VERSION #V1
        self.contrast_iou_thres   = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.IOU_THRESHOLD
        self.reweight_func        = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.REWEIGHT_FUNC

        self.cl_head_only         = cfg.MODEL.ROI_BOX_HEAD.CONTRASTIVE_BRANCH.HEAD_ONLY #false
        # fmt: off

        self.encoder = ContrastiveHead(self.fc_dim, self.mlp_head_dim)
        if self.loss_version == 'V1':
            self.criterion = SupConLoss(self.temperature, self.contrast_iou_thres, self.reweight_func)
        elif self.loss_version == 'V2':
            self.criterion = SupConLossV2(self.temperature, self.contrast_iou_thres)
        self.criterion.num_classes = self.num_classes  # to be used in protype version

    def _forward_box(self, features, proposals):
        box_features = self.box_pooler(features, [x.proposal_boxes for x in proposals])
        box_features = self.box_head(box_features)  # [None, FC_DIM]
        pred_class_logits, pred_proposal_deltas = self.box_predictor(box_features)
        box_features_contrast = self.encoder(box_features)
        del box_features

        if self.weight_decay:
            with EventStorage():
                storage = get_event_storage()
            if int(storage.iter) in self.decay_steps:
                self.contrast_loss_weight *= self.decay_rate

        outputs = FastRCNNContrastOutputs(
            self.box2box_transform,
            pred_class_logits,
            pred_proposal_deltas,
            proposals,
            self.smooth_l1_beta,
            box_features_contrast,
            self.criterion,
            self.contrast_loss_weight,
            self.box_reg_weight,
            # self.cl_head_only,
        )
        if self.training:
            return outputs.losses()
        else:
            pred_instances, _ = outputs.inference(
                self.test_score_thresh, self.test_nms_thresh, self.test_detections_per_img
            )
            return pred_instances
