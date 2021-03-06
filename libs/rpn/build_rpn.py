# # -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow.contrib.slim as slim
from libs.box_utils import make_rotate_anchors, nms_rotate
from libs.box_utils import coordinate_convert
from libs.box_utils import iou_rotate
from libs.box_utils import encode_and_decode
from libs.box_utils.show_box_in_tensor import *
from libs.losses import losses
from libs.configs import cfgs
from libs.box_utils import boxes_utils
DEBUG = True


class RPN(object):
    def __init__(self, net_name, inputs, gtboxes_and_label,
                 is_training,
                 share_net,
                 anchor_ratios,
                 anchor_scales,
                 anchor_angles,
                 scale_factors,
                 base_anchor_size_list,  # P2, P3, P4, P5, P6
                 level,
                 anchor_stride,
                 pool_stride,
                 top_k_nms,
                 kernel_size,
                 use_angles_condition,
                 anchor_angle_threshold,
                 nms_angle_threshold,
                 scope,
                 share_head=False,
                 rpn_nms_iou_threshold=0.7,
                 max_proposals_num=300,
                 rpn_iou_positive_threshold=0.7,
                 rpn_iou_negative_threshold=0.3,  # iou>=0.7 is positive box, iou< 0.3 is negative
                 rpn_mini_batch_size=256,
                 rpn_positives_ratio=0.5,
                 remove_outside_anchors=False,  # whether remove anchors outside
                 rpn_weight_decay=0.0001):

        self.net_name = net_name
        self.img_batch = inputs

        self.gtboxes_and_label = gtboxes_and_label  # shape is [M. 5],

        self.base_anchor_size_list = base_anchor_size_list
        self.level = level
        self.anchor_stride = anchor_stride,
        self.pool_stride = pool_stride,
        self.top_k_nms = top_k_nms
        self.kernel_size = kernel_size
        self.use_angles_condition = use_angles_condition
        self.anchor_angle_threshold = anchor_angle_threshold
        self.nms_angle_threshold = nms_angle_threshold
        self.anchor_ratios = anchor_ratios
        self.anchor_scales = anchor_scales
        self.anchor_angles = anchor_angles
        self.share_head = share_head
        self.scope = scope
        self.num_of_anchors_per_location = len(anchor_scales) * len(anchor_ratios) \
                                           * len(anchor_angles) * (pool_stride[0] / anchor_stride[0]) ** 2

        self.scale_factors = scale_factors

        self.rpn_nms_iou_threshold = rpn_nms_iou_threshold
        self.max_proposals_num = max_proposals_num

        self.rpn_iou_positive_threshold = rpn_iou_positive_threshold
        self.rpn_iou_negative_threshold = rpn_iou_negative_threshold
        self.rpn_mini_batch_size = rpn_mini_batch_size
        self.rpn_positives_ratio = rpn_positives_ratio
        self.remove_outside_anchors = remove_outside_anchors
        self.rpn_weight_decay = rpn_weight_decay
        self.is_training = is_training
        self.share_net = share_net

        self.feature_maps_dict = self.get_feature_maps()

        if cfgs.FEATURE_PYRAMID_MODE == 0:
            self.feature_pyramid = self.build_feature_pyramid()
        else:
            self.feature_pyramid = self.build_dense_feature_pyramid()

        self.anchors, self.rpn_encode_boxes, self.rpn_scores = self.get_anchors_and_rpn_predict()

    def get_feature_maps(self):

        '''
            Compared to https://github.com/KaimingHe/deep-residual-networks, the implementation of resnet_50 in slim
            subsample the output activations in the last residual unit of each block,
            instead of subsampling the input activations in the first residual unit of each block.
            The two implementations give identical results but the implementation of slim is more memory efficient.

            SO, when we build feature_pyramid, we should modify the value of 'C_*' to get correct spatial size feature maps.
            :return: feature maps
        '''

        with tf.variable_scope('get_feature_maps'):
            if self.net_name == 'resnet_v1_50':
                feature_maps_dict = {
                    'C2': self.share_net['{}resnet_v1_50/block1/unit_2/bottleneck_v1'.format(self.scope)],  # [56, 56]
                    'C3': self.share_net['{}resnet_v1_50/block2/unit_3/bottleneck_v1'.format(self.scope)],  # [28, 28]
                    'C4': self.share_net['{}resnet_v1_50/block3/unit_5/bottleneck_v1'.format(self.scope)],  # [14, 14]
                    # 'C5': self.share_net['{}resnet_v1_50/block4/unit_3/bottleneck_v1'.format(self.scope)],  # [7, 7]
                    'C5': self.share_net['resnet_v1_50/block4']  # [7, 7]]  # [7, 7]
                }
            elif self.net_name == 'resnet_v1_101':
                feature_maps_dict = {
                    'C2': self.share_net['{}resnet_v1_101/block1/unit_2/bottleneck_v1'.format(self.scope)],  # [56, 56]
                    'C3': self.share_net['{}resnet_v1_101/block2/unit_3/bottleneck_v1'.format(self.scope)],  # [28, 28]
                    'C4': self.share_net['{}resnet_v1_101/block3/unit_22/bottleneck_v1'.format(self.scope)],  # [14, 14]
                    'C5': self.share_net['resnet_v1_101/block4']  # [7, 7]
                }
            else:
                raise Exception('get no feature maps')

            return feature_maps_dict

    def build_dense_feature_pyramid(self):
        '''
        reference: DenseNet
        build P2, P3, P4, P5, P6
        :return: multi-scale feature map
        '''

        feature_pyramid = {}
        with tf.variable_scope('dense_feature_pyramid'):
            with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(self.rpn_weight_decay)):
                feature_pyramid['P5'] = slim.conv2d(self.feature_maps_dict['C5'],
                                                    num_outputs=256,
                                                    kernel_size=[1, 1],
                                                    stride=1,
                                                    scope='build_P5')

                feature_pyramid['P6'] = slim.max_pool2d(feature_pyramid['P5'],
                                                        kernel_size=[2, 2], stride=2, scope='build_P6')
                # P6 is down sample of P5

                for layer in range(4, 1, -1):
                    c = self.feature_maps_dict['C' + str(layer)]
                    c_conv = slim.conv2d(c, num_outputs=256, kernel_size=[1, 1], stride=1,
                                         scope='build_P%d/reduce_dimension' % layer)
                    p_concat = [c_conv]
                    up_sample_shape = tf.shape(c)
                    for layer_top in range(5, layer, -1):
                        p_temp = feature_pyramid['P' + str(layer_top)]

                        p_sub = tf.image.resize_nearest_neighbor(p_temp, [up_sample_shape[1], up_sample_shape[2]],
                                                                 name='build_P%d/up_sample_nearest_neighbor' % layer)
                        p_concat.append(p_sub)

                    p = tf.concat(p_concat, axis=3)

                    p_conv = slim.conv2d(p, 256, kernel_size=[3, 3], stride=[1, 1],
                                         padding='SAME', scope='build_P%d/avoid_aliasing' % layer)
                    feature_pyramid['P' + str(layer)] = p_conv

        return feature_pyramid

    def build_feature_pyramid(self):

        '''
        reference: https://github.com/CharlesShang/FastMaskRCNN
        build P2, P3, P4, P5, P6
        :return: multi-scale feature map
        '''

        feature_pyramid = {}
        with tf.variable_scope('feature_pyramid'):
            with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(self.rpn_weight_decay)):
                feature_pyramid['P5'] = slim.conv2d(self.feature_maps_dict['C5'],
                                                    num_outputs=256,
                                                    kernel_size=[1, 1],
                                                    stride=1,
                                                    scope='build_P5')

                feature_pyramid['P6'] = slim.max_pool2d(feature_pyramid['P5'],
                                                        kernel_size=[2, 2], stride=2, scope='build_P6')
                # P6 is down sample of P5

                for layer in range(4, 1, -1):
                    p, c = feature_pyramid['P' + str(layer + 1)], self.feature_maps_dict['C' + str(layer)]
                    up_sample_shape = tf.shape(c)
                    up_sample = tf.image.resize_nearest_neighbor(p, [up_sample_shape[1], up_sample_shape[2]],
                                                                 name='build_P%d/up_sample_nearest_neighbor' % layer)

                    c = slim.conv2d(c, num_outputs=256, kernel_size=[1, 1], stride=1,
                                    scope='build_P%d/reduce_dimension' % layer)
                    p = up_sample + c
                    p = slim.conv2d(p, 256, kernel_size=[3, 3], stride=1,
                                    padding='SAME', scope='build_P%d/avoid_aliasing' % layer)
                    feature_pyramid['P' + str(layer)] = p

        return feature_pyramid

    def make_rotate_anchors(self):
        with tf.variable_scope('make_rotate_anchors'):
            anchor_list = []
            level_list = self.level
            with tf.name_scope('make_rotate_anchors_all_level'):
                for level, base_anchor_size, pool_stride, anchor_stride \
                        in zip(level_list, self.base_anchor_size_list, self.pool_stride[0], self.anchor_stride[0]):
                    '''
                    original paper:
                    (level, base_anchor_size, stride) tuple:
                    (P2, 32, 4), (P3, 64, 8), (P4, 128, 16), (P5, 256, 32), (P6, 512, 64)

                    modification: each feature map point corresponds to two anchor lists to solve small target problem
                    (level, base_anchor_size, pool_stride, anchor_stride) tuple:
                    (P3, 32, 8, 4), (P4, 64, 16, 8)
                    '''
                    featuremap_height, featuremap_width = tf.shape(self.feature_pyramid[level])[1], \
                                                          tf.shape(self.feature_pyramid[level])[2]

                    featuremap_height = tf.multiply(tf.cast(featuremap_height, tf.float32),
                                                    tf.convert_to_tensor(pool_stride / anchor_stride, tf.float32))
                    featuremap_width = tf.multiply(tf.cast(featuremap_width, tf.float32),
                                                   tf.convert_to_tensor((pool_stride / anchor_stride), tf.float32))
                    tmp_anchors = make_rotate_anchors.make_anchors(base_anchor_size, self.anchor_scales,
                                                                   self.anchor_ratios, self.anchor_angles,
                                                                   featuremap_height,  featuremap_width, anchor_stride,
                                                                   name='make_anchors_{}'.format(level))

                    tmp_anchors = tf.reshape(tmp_anchors, [-1, 5])
                    anchor_list.append(tmp_anchors)

                all_level_anchors = tf.concat(anchor_list, axis=0)
            return all_level_anchors

    def rpn_net(self):

        rpn_encode_boxes_list = []
        rpn_scores_list = []
        with tf.variable_scope('rpn_net'):
            with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(self.rpn_weight_decay)):
                for level in self.level:

                    if self.share_head:
                        reuse_flag = None if level == 'P2' else True
                        scope_list = ['conv2d_3x3', 'rpn_classifier', 'rpn_regressor']
                        # in the begining, we should create variables, then sharing variables in P3, P4, P5
                    else:
                        reuse_flag = None
                        scope_list = ['conv2d_3x3_'+level, 'rpn_classifier_'+level, 'rpn_regressor_'+level]

                    rpn_conv2d_3x3 = slim.conv2d(inputs=self.feature_pyramid[level],
                                                 num_outputs=256,
                                                 kernel_size=[self.kernel_size, self.kernel_size],
                                                 stride=1,
                                                 scope=scope_list[0],
                                                 reuse=reuse_flag)

                    rpn_box_scores = slim.conv2d(rpn_conv2d_3x3,
                                                 num_outputs=2 * self.num_of_anchors_per_location,
                                                 kernel_size=[1, 1],
                                                 stride=1,
                                                 scope=scope_list[1],
                                                 activation_fn=None,
                                                 reuse=reuse_flag)
                    rpn_encode_boxes = slim.conv2d(rpn_conv2d_3x3,
                                                   num_outputs=5 * self.num_of_anchors_per_location,
                                                   kernel_size=[1, 1],
                                                   stride=1,
                                                   scope=scope_list[2],
                                                   activation_fn=None,
                                                   reuse=reuse_flag)

                    rpn_box_scores = tf.reshape(rpn_box_scores, [-1, 2])
                    rpn_encode_boxes = tf.reshape(rpn_encode_boxes, [-1, 5])

                    rpn_scores_list.append(rpn_box_scores)
                    rpn_encode_boxes_list.append(rpn_encode_boxes)

                rpn_all_encode_boxes = tf.concat(rpn_encode_boxes_list, axis=0)
                rpn_all_boxes_scores = tf.concat(rpn_scores_list, axis=0)

            return rpn_all_encode_boxes, rpn_all_boxes_scores

    def get_anchors_and_rpn_predict(self):

        anchors = self.make_rotate_anchors()
        rpn_encode_boxes, rpn_scores = self.rpn_net()

        with tf.name_scope('get_anchors_and_rpn_predict'):
            if self.is_training:
                if self.remove_outside_anchors:
                    anchors_convert = tf.py_func(coordinate_convert.forward_convert,
                                                 inp=[anchors],
                                                 Tout=tf.float32)
                    anchors_convert = tf.reshape(anchors_convert, [-1, 8])
                    valid_indices = boxes_utils.filter_outside_boxes(boxes=anchors_convert,
                                                                     img_h=tf.shape(self.img_batch)[1],
                                                                     img_w=tf.shape(self.img_batch)[2])
                    valid_anchors = tf.gather(anchors, valid_indices)
                    rpn_valid_encode_boxes = tf.gather(rpn_encode_boxes, valid_indices)
                    rpn_valid_scores = tf.gather(rpn_scores, valid_indices)

                    return valid_anchors, rpn_valid_encode_boxes, rpn_valid_scores

                else:
                    return anchors, rpn_encode_boxes, rpn_scores
            else:
                return anchors, rpn_encode_boxes, rpn_scores

    def rpn_find_positive_negative_samples(self, anchors):
        '''
        assign anchors targets: object or background.
        :param anchors: [valid_num_of_anchors, 5]. use N to represent valid_num_of_anchors

        :return:labels. anchors_matched_gtboxes, object_mask

        labels shape is [N, ].  positive is 1, negative is 0, ignored is -1
        anchor_matched_gtboxes. each anchor's gtbox(only positive box has gtbox)shape is [N, 5]
        object_mask. tf.float32. 1.0 represent box is object, 0.0 is others. shape is [N, ]
        '''
        with tf.variable_scope('rpn_find_positive_negative_samples'):
            gtboxes = tf.reshape(self.gtboxes_and_label[:, :-1], [-1, 5])
            gtboxes = tf.cast(gtboxes, tf.float32)

            # ious = iou.iou_calculate(anchors, gtboxes)  # [N, M]
            ious = iou_rotate.iou_rotate_calculate(anchors, gtboxes, use_gpu=cfgs.IOU_USE_GPU, gpu_id=0)

            # ious = tf.py_func(demo.iou_rotate_calculate,
            #                   inp=[anchors, gtboxes],
            #                   Tout=tf.float32)

            ious = tf.reshape(ious, [tf.shape(anchors)[0], tf.shape(gtboxes)[0]])

            max_iou_each_row = tf.reduce_max(ious, axis=1)

            labels = tf.ones(shape=[tf.shape(anchors)[0], ], dtype=tf.float32) * (-1)  # [N, ] # ignored is -1

            matchs = tf.cast(tf.argmax(ious, axis=1), tf.int32)
            # matchs = matchs * tf.cast(positives, dtype=matchs.dtype)  # remove background and ignored
            anchors_matched_gtboxes = tf.gather(gtboxes, matchs)  # [N, 5]

            negatives = tf.less(max_iou_each_row, self.rpn_iou_negative_threshold)
            negatives = tf.logical_and(negatives, tf.greater(max_iou_each_row, 0.1))

            if self.use_angles_condition:
                # an anchor that has an IoU overlap higher than 0.7 with any ground-truth box
                cond1 = tf.greater_equal(max_iou_each_row, self.rpn_iou_positive_threshold)  # iou >= 0.7 is positive

                # angle condition
                gtboxes_angles = anchors_matched_gtboxes[:, -1]  # tf.unstack(anchors_matched_gtboxes, axis=1)
                anchors_angles = anchors[:, -1]  # tf.unstack(anchors, axis=1)
                cond2 = tf.less_equal(tf.abs(gtboxes_angles-anchors_angles), self.anchor_angle_threshold)
                cond3 = tf.greater(tf.abs(gtboxes_angles - anchors_angles), self.anchor_angle_threshold)

                positives1 = tf.logical_and(cond1, cond2)
                negatives = tf.logical_or(negatives, tf.logical_and(cond1, cond3))
            else:
                positives1 = tf.greater_equal(max_iou_each_row, self.rpn_iou_positive_threshold)

            # to avoid none of boxes iou >= 0.7, use max iou boxes as positive
            max_iou_each_column = tf.reduce_max(ious, 0)
            # the anchor/anchors with the highest Intersection-over-Union (IoU) overlap with a ground-truth box
            positives2 = tf.reduce_sum(tf.cast(tf.equal(ious, max_iou_each_column), tf.float32), axis=1)

            positives = tf.logical_or(positives1, tf.cast(positives2, tf.bool))

            labels += 2 * tf.cast(positives, tf.float32)  # Now, positive is 1, ignored and background is -1

            # object_mask = tf.cast(positives, tf.float32)  # 1.0 is object, 0.0 is others
            # background's gtboxes tmp set the first gtbox, it dose not matter, because use object_mask will ignored it

            labels += tf.cast(negatives, tf.float32)  # [N, ] positive is >=1.0, negative is 0, ignored is -1.0
            '''
            Need to note: when opsitive, labels may >= 1.0.
            Because, when all the iou< 0.7, we set anchors having max iou each column as positive.
            these anchors may have iou < 0.3.
            In the begining, labels is [-1, -1, -1...-1]
            then anchors having iou<0.3 as well as are max iou each column will be +1.0.
            when decide negatives, because of iou<0.3, they add 1.0 again.
            So, the final result will be 2.0

            So, when opsitive, labels may in [1.0, 2.0]. that is labels >=1.0
            '''
            positives = tf.cast(tf.greater_equal(labels, 1.0), tf.float32)
            ignored = tf.cast(tf.equal(labels, -1.0), tf.float32) * -1

            labels = positives + ignored
            object_mask = tf.cast(positives, tf.float32)  # 1.0 is object, 0.0 is others

            return labels, anchors_matched_gtboxes, object_mask

    def make_minibatch(self, valid_anchors):
        with tf.variable_scope('rpn_minibatch'):

            # in labels(shape is [N, ]): 1 is positive, 0 is negative, -1 is ignored
            labels, anchor_matched_gtboxes, object_mask = \
                self.rpn_find_positive_negative_samples(valid_anchors)  # [num_of_valid_anchors, ]

            positive_indices = tf.reshape(tf.where(tf.equal(labels, 1.0)), [-1])  # use labels is same as object_mask

            num_of_positives = tf.minimum(tf.shape(positive_indices)[0],
                                          tf.cast(self.rpn_mini_batch_size * self.rpn_positives_ratio, tf.int32))

            # num of positives <= minibatch_size * 0.5
            positive_indices = tf.random_shuffle(positive_indices)
            positive_indices = tf.slice(positive_indices, begin=[0], size=[num_of_positives])

            negative_indices = tf.reshape(tf.where(tf.equal(labels, 0.0)), [-1])
            num_of_negatives = tf.minimum(self.rpn_mini_batch_size - num_of_positives,
                                          tf.shape(negative_indices)[0])

            negative_indices = tf.random_shuffle(negative_indices)
            negative_indices = tf.slice(negative_indices, begin=[0], size=[num_of_negatives])

            minibatch_indices = tf.concat([positive_indices, negative_indices], axis=0)
            minibatch_indices = tf.random_shuffle(minibatch_indices)

            minibatch_anchor_matched_gtboxes = tf.gather(anchor_matched_gtboxes, minibatch_indices)
            object_mask = tf.gather(object_mask, minibatch_indices)
            labels = tf.cast(tf.gather(labels, minibatch_indices), tf.int32)
            labels_one_hot = tf.one_hot(labels, depth=2)
            return minibatch_indices, minibatch_anchor_matched_gtboxes, object_mask, labels_one_hot

    def rpn_losses(self):
        with tf.variable_scope('rpn_losses'):
            minibatch_indices, minibatch_anchor_matched_gtboxes, \
            object_mask, minibatch_labels_one_hot = self.make_minibatch(self.anchors)

            minibatch_anchors = tf.gather(self.anchors, minibatch_indices)
            minibatch_encode_boxes = tf.gather(self.rpn_encode_boxes, minibatch_indices)
            minibatch_boxes_scores = tf.gather(self.rpn_scores, minibatch_indices)

            # encode gtboxes
            minibatch_encode_gtboxes = encode_and_decode.encode_boxes(unencode_boxes=minibatch_anchor_matched_gtboxes,
                                                                      reference_boxes=minibatch_anchors,
                                                                      scale_factors=self.scale_factors)

            positive_anchors_in_img = draw_box_with_color(self.img_batch,
                                                          minibatch_anchors * tf.expand_dims(object_mask, 1),
                                                          text=tf.shape(tf.where(tf.equal(object_mask, 1.0)))[0])

            negative_mask = tf.cast(tf.logical_not(tf.cast(object_mask, tf.bool)), tf.float32)
            negative_anchors_in_img = draw_box_with_color(self.img_batch,
                                                          minibatch_anchors * tf.expand_dims(negative_mask, 1),
                                                          text=tf.shape(tf.where(tf.equal(object_mask, 0.0)))[0])

            minibatch_decode_boxes = encode_and_decode.decode_boxes(encode_boxes=minibatch_encode_boxes,
                                                                    reference_boxes=minibatch_anchors,
                                                                    scale_factors=self.scale_factors)

            tf.summary.image('/positive_anchors', positive_anchors_in_img)
            tf.summary.image('/negative_anchors', negative_anchors_in_img)

            minibatch_boxes_softmax_scores = tf.gather(slim.softmax(self.rpn_scores), minibatch_indices)
            top_k_scores, top_k_indices = tf.nn.top_k(minibatch_boxes_softmax_scores[:, 1], k=20)

            top_k_boxes = tf.gather(minibatch_decode_boxes, top_k_indices)
            top_detections_in_img = draw_boxes_with_scores(self.img_batch,
                                                           boxes=top_k_boxes,
                                                           scores=top_k_scores)

            tf.summary.image('/top_20', top_detections_in_img)

            temp_indices = tf.reshape(tf.where(tf.greater(top_k_scores, cfgs.FINAL_SCORE_THRESHOLD)), [-1])
            rpn_predict_boxes = tf.gather(top_k_boxes, temp_indices)
            rpn_predict_scores = tf.gather(top_k_scores, temp_indices)

            # losses
            with tf.variable_scope('rpn_location_loss'):
                location_loss = losses.l1_smooth_losses(predict_boxes=minibatch_encode_boxes,
                                                        gtboxes=minibatch_encode_gtboxes,
                                                        object_weights=object_mask)
                slim.losses.add_loss(location_loss)  # add smooth l1 loss to losses collection

            with tf.variable_scope('rpn_classification_loss'):
                classification_loss = slim.losses.softmax_cross_entropy(logits=minibatch_boxes_scores,
                                                                        onehot_labels=minibatch_labels_one_hot)

            return location_loss, classification_loss, rpn_predict_boxes, rpn_predict_scores

    def rpn_proposals(self):
        with tf.variable_scope('rpn_proposals'):
            rpn_decode_boxes = encode_and_decode.decode_boxes(encode_boxes=self.rpn_encode_boxes,
                                                              reference_boxes=self.anchors,
                                                              scale_factors=self.scale_factors)

            # if not self.is_training:  # when test, clip proposals to img boundaries
            #     img_shape = tf.shape(self.img_batch)
            #     rpn_decode_boxes = boxes_utils.clip_boxes_to_img_boundaries(rpn_decode_boxes, img_shape)

            rpn_softmax_scores = slim.softmax(self.rpn_scores)
            rpn_object_score = rpn_softmax_scores[:, 1]  # second column represent object

            if self.top_k_nms:
                rpn_object_score, top_k_indices = tf.nn.top_k(rpn_object_score, k=self.top_k_nms)
                rpn_decode_boxes = tf.gather(rpn_decode_boxes, top_k_indices)

            if not cfgs.USE_HORIZONTAL_NMS:
                valid_indices = nms_rotate.nms_rotate(decode_boxes=rpn_decode_boxes,
                                                      scores=rpn_object_score,
                                                      iou_threshold=self.rpn_nms_iou_threshold,
                                                      max_output_size=self.max_proposals_num,
                                                      use_angle_condition=self.use_angles_condition,
                                                      angle_threshold=self.anchor_angle_threshold,
                                                      use_gpu=cfgs.NMS_USE_GPU)

            ############################################################################################################
            else:
                rpn_decode_boxes_convert = tf.py_func(coordinate_convert.forward_convert,
                                                      inp=[rpn_decode_boxes],
                                                      Tout=tf.float32)

                rpn_decode_boxes_convert = tf.reshape(rpn_decode_boxes_convert, [tf.shape(rpn_decode_boxes)[0], 8])
                x1, y1, x2, y2, x3, y3, x4, y4 = tf.unstack(rpn_decode_boxes_convert, axis=1)
                x = tf.transpose(tf.stack([x1, x2, x3, x4]))
                y = tf.transpose(tf.stack([y1, y2, y3, y4]))
                min_x = tf.reduce_min(x, axis=1)
                max_x = tf.reduce_max(x, axis=1)
                min_y = tf.reduce_min(y, axis=1)
                max_y = tf.reduce_max(y, axis=1)
                rpn_decode_boxes_convert = tf.transpose(tf.stack([min_x, min_y, max_x, max_y]))

                valid_indices = tf.image.non_max_suppression(boxes=rpn_decode_boxes_convert,
                                                             scores=rpn_object_score,
                                                             max_output_size=self.max_proposals_num,
                                                             iou_threshold=self.rpn_nms_iou_threshold,
                                                             name='rpn_horizontal_nms')

            ############################################################################################################

            valid_boxes = tf.gather(rpn_decode_boxes, valid_indices)
            valid_scores = tf.gather(rpn_object_score, valid_indices)
            rpn_proposals_boxes, rpn_proposals_scores = tf.cond(
                tf.less(tf.shape(valid_boxes)[0], self.max_proposals_num),
                lambda: boxes_utils.padd_boxes_with_zeros(valid_boxes, valid_scores,
                                                          self.max_proposals_num),
                lambda: (valid_boxes, valid_scores))

            return rpn_proposals_boxes, rpn_proposals_scores
