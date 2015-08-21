# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Transform a roidb into a trainable roidb by adding a bunch of metadata."""

import numpy as np
from fast_rcnn.config import cfg
import utils.cython_bbox
import scipy.sparse

def prepare_roidb(imdb):
    """Enrich the imdb's roidb by adding some derived quantities that
    are useful for training. This function precomputes the maximum
    overlap, taken over ground-truth boxes, between each ROI and
    each ground-truth box. The class with maximum overlap is also
    recorded.
    """
    roidb = imdb.roidb
    for i in xrange(len(imdb.image_index)):
        roidb[i]['image'] = imdb.image_path_at(i)
        # need gt_overlaps as a dense array for argmax
        gt_overlaps = roidb[i]['gt_overlaps'].toarray()
        gt_subindexes = roidb[i]['gt_subindexes']
        # max overlap with gt over classes (columns)
        max_overlaps = gt_overlaps.max(axis=1)
        # gt class that had the max overlap
        max_classes = gt_overlaps.argmax(axis=1)
        max_subclasses = np.zeros(max_classes.shape, dtype=np.int32)
        for j in range(len(max_classes)):
            max_subclasses[j] = gt_subindexes[j, max_classes[j]]
        roidb[i]['max_classes'] = max_classes
        roidb[i]['max_subclasses'] = max_subclasses
        roidb[i]['max_overlaps'] = max_overlaps
        # sanity checks
        # max overlap of 0 => class should be zero (background)
        zero_inds = np.where(max_overlaps == 0)[0]
        assert all(max_classes[zero_inds] == 0)
        assert all(max_subclasses[zero_inds] == 0)
        # max overlap > 0 => class should not be zero (must be a fg class)
        nonzero_inds = np.where(max_overlaps > 0)[0]
        assert all(max_classes[nonzero_inds] != 0)
        assert all(max_subclasses[nonzero_inds] != 0)

def add_bbox_regression_targets(roidb, boxes_grid):
    """Add information needed to train bounding-box regressors."""
    assert len(roidb) > 0
    assert 'max_classes' in roidb[0], 'Did you call prepare_roidb first?'

    num_images = len(roidb)
    # Infer number of classes from the number of columns in gt_overlaps
    num_classes = roidb[0]['gt_overlaps'].shape[1]

    for im_i in xrange(num_images):
        boxes_all = roidb[im_i]['boxes_all']
        gt_overlaps_grid = roidb[im_i]['gt_overlaps_grid'].toarray()
        gt_classes = roidb[im_i]['gt_classes']
        gt_classes_all = np.tile(gt_classes, len(cfg.TRAIN.SCALES))
        roidb[im_i]['bbox_targets'] = \
                _compute_targets(boxes_grid, boxes_all, gt_overlaps_grid, gt_classes_all)

    # Compute values needed for means and stds
    # var(x) = E(x^2) - E(x)^2
    class_counts = np.zeros((num_classes, 1)) + cfg.EPS
    sums = np.zeros((num_classes, 4))
    squared_sums = np.zeros((num_classes, 4))
    for im_i in xrange(num_images):
        targets = roidb[im_i]['bbox_targets']
        for cls in xrange(1, num_classes):
            cls_inds = np.where(targets[:, 0] == cls)[0]
            if cls_inds.size > 0:
                class_counts[cls] += cls_inds.size
                sums[cls, :] += targets[cls_inds, 1:].sum(axis=0)
                squared_sums[cls, :] += (targets[cls_inds, 1:] ** 2).sum(axis=0)

    means = sums / class_counts
    stds = np.sqrt(squared_sums / class_counts - means ** 2)

    # Normalize targets
    for im_i in xrange(num_images):
        targets = roidb[im_i]['bbox_targets']
        for cls in xrange(1, num_classes):
            cls_inds = np.where(targets[:, 0] == cls)[0]
            roidb[im_i]['bbox_targets'][cls_inds, 1:] -= means[cls, :]
            if stds[cls, 0] != 0:
                roidb[im_i]['bbox_targets'][cls_inds, 1:] /= stds[cls, :]
        # save sparse matrix
        targets = roidb[im_i]['bbox_targets']
        roidb[im_i]['bbox_targets'] = scipy.sparse.csr_matrix(targets)

    # These values will be needed for making predictions
    # (the predicts will need to be unnormalized and uncentered)
    return means.ravel(), stds.ravel()

def _compute_targets(boxes_grid, boxes_all, gt_overlaps_grid, gt_classes_all):
    """Compute bounding-box regression targets for an image."""
    if gt_overlaps_grid.shape[1] == 0:
        return np.zeros((boxes_grid.shape[0], 5), dtype=np.float32)

    max_overlaps = gt_overlaps_grid.max(axis = 1)
    argmax_overlaps = gt_overlaps_grid.argmax(axis = 1)

    # Indices of examples for which we try to make predictions
    ex_inds = np.where(max_overlaps >= cfg.TRAIN.BBOX_THRESH)[0]
    gt_inds = argmax_overlaps[ex_inds]

    gt_rois = boxes_all[gt_inds, :]
    ex_rois = boxes_grid[ex_inds, :]

    ex_widths = ex_rois[:, 2] - ex_rois[:, 0] + cfg.EPS
    ex_heights = ex_rois[:, 3] - ex_rois[:, 1] + cfg.EPS
    ex_ctr_x = ex_rois[:, 0] + 0.5 * ex_widths
    ex_ctr_y = ex_rois[:, 1] + 0.5 * ex_heights

    gt_widths = gt_rois[:, 2] - gt_rois[:, 0] + cfg.EPS
    gt_heights = gt_rois[:, 3] - gt_rois[:, 1] + cfg.EPS
    gt_ctr_x = gt_rois[:, 0] + 0.5 * gt_widths
    gt_ctr_y = gt_rois[:, 1] + 0.5 * gt_heights

    targets_dx = (gt_ctr_x - ex_ctr_x) / ex_widths
    targets_dy = (gt_ctr_y - ex_ctr_y) / ex_heights
    targets_dw = np.log(gt_widths / ex_widths)
    targets_dh = np.log(gt_heights / ex_heights)

    targets = np.zeros((boxes_grid.shape[0], 5), dtype=np.float32)
    targets[ex_inds, 0] = gt_classes_all[gt_inds]
    targets[ex_inds, 1] = targets_dx
    targets[ex_inds, 2] = targets_dy
    targets[ex_inds, 3] = targets_dw
    targets[ex_inds, 4] = targets_dh
    return targets