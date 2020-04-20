# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

import numpy as np
import torch
from torch.autograd import Variable

from ..network import np_to_variable


# from sympy.physics.paulialgebra import delta


def bbox_transform(ex_rois, gt_rois):
    """
    computes the distance from ground-truth boxes to the given boxes, normed by their size
    :param ex_rois: n * 4 numpy array, given boxes
    :param gt_rois: n * 4 numpy array, ground-truth boxes
    :return: deltas: n * 4 numpy array, ground-truth boxes
    """
    ex_widths = ex_rois[:, 2] - ex_rois[:, 0] + 1.0
    ex_heights = ex_rois[:, 3] - ex_rois[:, 1] + 1.0
    ex_ctr_x = ex_rois[:, 0] + 0.5 * ex_widths
    ex_ctr_y = ex_rois[:, 1] + 0.5 * ex_heights

    # assert np.min(ex_widths) > 0.1 and np.min(ex_heights) > 0.1, \
    #     'Invalid boxes found: {} {}'. \
    #         format(ex_rois[np.argmin(ex_widths), :], ex_rois[np.argmin(ex_heights), :])

    gt_widths = gt_rois[:, 2] - gt_rois[:, 0] + 1.0
    gt_heights = gt_rois[:, 3] - gt_rois[:, 1] + 1.0
    gt_ctr_x = gt_rois[:, 0] + 0.5 * gt_widths
    gt_ctr_y = gt_rois[:, 1] + 0.5 * gt_heights

    targets_dx = (gt_ctr_x - ex_ctr_x) / ex_widths
    targets_dy = (gt_ctr_y - ex_ctr_y) / ex_heights
    targets_dw = np.log(gt_widths / ex_widths)
    targets_dh = np.log(gt_heights / ex_heights)

    targets = np.vstack(
        (targets_dx, targets_dy, targets_dw, targets_dh)).transpose()
    return targets


def bbox_transform_inv(boxes, deltas):
    if boxes.shape[0] == 0:
        return np.zeros((0,), dtype=deltas.dtype)

    boxes = boxes.astype(deltas.dtype, copy=False)

    widths = boxes[:, 2] - boxes[:, 0] + 1.0
    heights = boxes[:, 3] - boxes[:, 1] + 1.0
    ctr_x = boxes[:, 0] + 0.5 * widths
    ctr_y = boxes[:, 1] + 0.5 * heights

    dx = deltas[:, 0::4]
    dy = deltas[:, 1::4]
    dw = deltas[:, 2::4]
    dh = deltas[:, 3::4]

    pred_ctr_x = dx * widths[:, np.newaxis] + ctr_x[:, np.newaxis]
    pred_ctr_y = dy * heights[:, np.newaxis] + ctr_y[:, np.newaxis]
    pred_w = np.exp(dw) * widths[:, np.newaxis]
    pred_h = np.exp(dh) * heights[:, np.newaxis]

    pred_boxes = np.zeros(deltas.shape, dtype=deltas.dtype)
    # x1
    pred_boxes[:, 0::4] = pred_ctr_x - 0.5 * pred_w
    # y1
    pred_boxes[:, 1::4] = pred_ctr_y - 0.5 * pred_h
    # x2
    pred_boxes[:, 2::4] = pred_ctr_x + 0.5 * pred_w
    # y2
    pred_boxes[:, 3::4] = pred_ctr_y + 0.5 * pred_h

    return pred_boxes


def clip_boxes(boxes, im_shape):
    """
    Clip boxes to image boundaries.
    """
    if boxes.shape[0] == 0:
        return boxes

    # x1 >= 0
    boxes[:, 0::4] = np.maximum(np.minimum(boxes[:, 0::4], im_shape[1] - 1), 0)
    # y1 >= 0
    boxes[:, 1::4] = np.maximum(np.minimum(boxes[:, 1::4], im_shape[0] - 1), 0)
    # x2 < im_shape[1]
    boxes[:, 2::4] = np.maximum(np.minimum(boxes[:, 2::4], im_shape[1] - 1), 0)
    # y2 < im_shape[0]
    boxes[:, 3::4] = np.maximum(np.minimum(boxes[:, 3::4], im_shape[0] - 1), 0)
    return boxes


def enlarge_clip_rois(rois, im_shape, ratio):
    # enlarge
    x0 = rois[:, 1] - 0.5 * (ratio - 1) * (rois[:, 3] - rois[:, 1])
    y0 = rois[:, 2] - 0.5 * (ratio - 1) * (rois[:, 4] - rois[:, 2])
    x1 = rois[:, 3] + 0.5 * (ratio - 1) * (rois[:, 3] - rois[:, 1])
    y1 = rois[:, 4] + 0.5 * (ratio - 1) * (rois[:, 4] - rois[:, 2])
    # clip
    zeros = Variable(torch.zeros(rois.size(0))).cuda()
    im_shape = np_to_variable(im_shape).repeat(rois.size(0), 1)
    x2 = torch.max(torch.min(x0, im_shape[:, 1] - 1), zeros)
    y2 = torch.max(torch.min(y0, rois[:, 4]), zeros)
    x3 = torch.max(torch.min(x1, im_shape[:, 1] - 1), zeros)
    y3 = torch.max(torch.min(y1, rois[:, 4]), zeros)

    return torch.stack([rois[:, 0], x2, y2, x3, y3], 1)


def transform_deltas(proposals, ctx_loc, pred_deltas):

    """
    Here to regress the transformed deltas fo every context bins.

    proposal: Variable.  shape: num_rois*5, (0, x1, y1, x2, y2)
    ctx_loc:  Variable   shape: num_rois*num_bins*5,   (0, x1, y1, x2, y2)
    pred_deltas: Variable. shape num_rois*num_bins*4,  (x1_delta, y1_delta, x2_delta, y2_delta)
    """

    ctx_loc = ctx_loc.detach()
    proposals = proposals.detach()

    num_rois, num_bins = ctx_loc.shape[:2]
    proposals = proposals[:, 1:]

    proposals_wh = -proposals[:, :2] + proposals[:, 2:]
    proposals_wh = proposals_wh + 1.0
    proposals_xy = proposals[:, :2] + proposals_wh / 2.0  # dx, dy
    proposals_xywh = torch.cat((proposals_xy, proposals_wh), 1)

    proposals_xywh = proposals_xywh.unsqueeze(1).repeat(1, num_bins, 1).view(num_rois*num_bins, -1)

    ctx_loc = ctx_loc[:, :, 1:]
    ctx_loc_wh = -ctx_loc[:, :, :2] + ctx_loc[:, :, 2:] # dw, dh
    ctx_loc_wh = ctx_loc_wh + 1.0
    ctx_loc_xy = ctx_loc[:, :, :2] + ctx_loc_wh / 2.0  # dx, dy

    ctx_loc_xywh = torch.cat((ctx_loc_xy, ctx_loc_wh), 2).contiguous().view(num_rois*num_bins, -1)

    pred_deltas = pred_deltas.contiguous().view(num_rois*num_bins, -1)
    pred_deltas_xy = pred_deltas[:, :2] * ctx_loc_xywh[:, 2:] / proposals_xywh[:, 2:]

    pred_deltas_total = torch.cat((pred_deltas_xy, pred_deltas[:, 2:]), 1)

    trans_deltas_wh = torch.log(ctx_loc_xywh[:, 2:])-torch.log(proposals_xywh[:, 2:])
    trans_deltas_xy = (ctx_loc_xywh[:, :2] - proposals_xywh[:, :2]) / proposals_xywh[:, 2:]

    trans_deltas = pred_deltas_total + torch.cat((trans_deltas_xy, trans_deltas_wh), 1)

    return trans_deltas


def inverse_transform_deltas(pred_deltas, proposals, ctx_loc):

    """
        Here to regress the transformed deltas fo every context bins.

        proposal: Variable.  shape: num_rois*5, (0, x1, y1, x2, y2)
        ctx_loc:  Variable   shape: num_rois*num_bins*5,   (0, x1, y1, x2, y2)
        pred_deltas: Variable. shape num_rois*num_bins*4,  (x1_delta, y1_delta, x2_delta, y2_delta)
    """

    proposals = proposals.data.cpu().numpy()
    ctx_loc = ctx_loc.data.cpu().numpy()

    num_rois, num_bins = ctx_loc.shape[:2]

    pred_deltas = pred_deltas.contiguous().view(num_rois*num_bins, -1)

    proposals = proposals[:, 1:]
    proposals[:, 2:] = -proposals[:, :2] + proposals[:, 2:]  # dw, dh
    proposals[:, :2] = proposals[:, :2] + proposals[:, 2:] / 2.0  # dx, dy
    proposals = proposals[:, None, :].repeat(num_bins, 1).reshape(num_rois * num_bins, -1)  # 18*4

    ctx_loc = ctx_loc[:, :, 1:]
    ctx_loc[:, :, 2:] = -ctx_loc[:, :, :2] + ctx_loc[:, :, 2:]  # dw, dh
    ctx_loc[:, :, :2] = ctx_loc[:, :, :2] + ctx_loc[:, :, 2:] / 2.0  # dx, dy
    ctx_loc = ctx_loc.reshape(num_rois * num_bins, -1)


    pred_deltas_xy = pred_deltas[:,:2] * Variable(torch.FloatTensor((proposals[:, 2:] / ctx_loc[:, 2:]))).cuda()

    pred_totals = torch.cat((pred_deltas_xy, pred_deltas[:, 2:]), 1)

    trans_deltas = np.zeros_like(proposals)


    trans_deltas[:, :2] = (proposals[:, :2] - ctx_loc[:, :2])/ctx_loc[:, 2:]
    trans_deltas[:, 2:] = np.log(proposals[:, 2:]) - np.log(ctx_loc[:, 2:])
    pred_totals += Variable(torch.FloatTensor(trans_deltas)).cuda()

    return pred_totals


def inverse_transform_deltas_variable(pred_deltas, proposals, ctx_loc):
    """
        Here to regress the transformed deltas fo every context bins.

        proposal: Variable.  shape: num_rois*5, (0, x1, y1, x2, y2)
        ctx_loc:  Variable   shape: num_rois*num_bins*5,   (0, x1, y1, x2, y2)
        pred_deltas: Variable. shape num_rois*num_bins*4,  (x_delta, y_delta, w_delta, y_delta)
    """

    num_rois, num_bins = ctx_loc.shape[:2]

    pred_dt = pred_deltas.unsqueeze(1).repeat(1, num_bins, 1).contiguous().view(num_rois*num_bins, -1)
    proposals = proposals[:, 1:]

    proposals_wh = -proposals[:, :2] + proposals[:, 2:]
    proposals_xy = proposals[:, :2] + proposals_wh / 2.0

    proposals_xywh = torch.cat((proposals_xy, proposals_wh), 1)
    proposals_xywh = proposals_xywh.unsqueeze(1).repeat(1, num_bins, 1).contiguous().view(num_rois * num_bins, -1)


    ctx_loc = ctx_loc[:, :, 1:]
    ctx_loc_wh = -ctx_loc[:, :, :2] + ctx_loc[:, :, 2:]
    ctx_loc_xy = ctx_loc[:, :, :2] + ctx_loc_wh / 2.0
    ctx_loc_xywh = torch.cat((ctx_loc_xy, ctx_loc_wh), 2)
    ctx_loc_xywh = ctx_loc_xywh.contiguous().view(num_rois * num_bins, -1)

    pred_deltas_xy = pred_dt[:, :2] * proposals_xywh[:, 2:]/ctx_loc_xywh[:, 2:]

    pred_totals = torch.cat((pred_deltas_xy, pred_dt[:, 2:]), 1)


    trans_deltas_xy = (proposals_xywh[:, :2] - ctx_loc_xywh[:, :2]) / ctx_loc_xywh[:, 2:]
    trans_deltas_wh = torch.log(proposals_xywh[:, 2:]) - torch.log(ctx_loc_xywh[:, 2:])
    pred_totals += torch.cat((trans_deltas_xy, trans_deltas_wh), 1)

    return pred_totals


