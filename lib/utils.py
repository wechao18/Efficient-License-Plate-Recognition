from __future__ import division

import torch 
import torch.nn as nn
import torch.nn.functional as F 
from torch.autograd import Variable
import numpy as np
import cv2 
import matplotlib.pyplot as plt
from lib.bbox import bbox_iou

class SwishImplementation(torch.autograd.Function):
    @staticmethod
    def forward(ctx, i):
        result = i * torch.sigmoid(i)
        ctx.save_for_backward(i)
        return result

    @staticmethod
    def backward(ctx, grad_output):
        i = ctx.saved_variables[0]
        sigmoid_i = torch.sigmoid(i)
        return grad_output * (sigmoid_i * (1 + i * (1 - sigmoid_i)))


class MemoryEfficientSwish(nn.Module):
    def forward(self, x):
        return SwishImplementation.apply(x)


class Swish(nn.Module):
    def forward(self, x):
        return x * torch.sigmoid(x)

def count_parameters(model):
    return sum(p.numel() for p in model.parameters())

def count_learnable_parameters(model):
    return sum(p.numel() for p in model.parameters() if p.requires_grad)

def convert2cpu(matrix):
    if matrix.is_cuda:
        return torch.FloatTensor(matrix.size()).copy_(matrix)
    else:
        return matrix

def predict_transform(prediction, inp_dim, anchors, num_classes, CUDA = True):
    batch_size = prediction.size(0)
    stride =  inp_dim // prediction.size(2)
    grid_size = inp_dim // stride
    bbox_attrs = 5 + num_classes
    num_anchors = len(anchors)
    
    anchors = [(a[0]/stride, a[1]/stride) for a in anchors]

    prediction = prediction.view(batch_size, bbox_attrs*num_anchors, grid_size*grid_size)
    prediction = prediction.transpose(1,2).contiguous()
    prediction = prediction.view(batch_size, grid_size*grid_size*num_anchors, bbox_attrs)

    #Sigmoid the  centre_X, centre_Y. and object confidencce
    prediction[:,:,0] = torch.sigmoid(prediction[:,:,0])
    prediction[:,:,1] = torch.sigmoid(prediction[:,:,1])
    prediction[:,:,4] = torch.sigmoid(prediction[:,:,4])
    
    #Add the center offsets
    grid_len = np.arange(grid_size)
    a,b = np.meshgrid(grid_len, grid_len)
    
    x_offset = torch.FloatTensor(a).view(-1,1)
    y_offset = torch.FloatTensor(b).view(-1,1)
    
    if CUDA:
        x_offset = x_offset.cuda()
        y_offset = y_offset.cuda()
    
    x_y_offset = torch.cat((x_offset, y_offset), 1).repeat(1,num_anchors).view(-1,2).unsqueeze(0)
    
    prediction[:,:,:2] += x_y_offset
      
    #log space transform height and the width
    anchors = torch.FloatTensor(anchors)
    
    if CUDA:
        anchors = anchors.cuda()
    
    anchors = anchors.repeat(grid_size*grid_size, 1).unsqueeze(0)
    prediction[:,:,2:4] = torch.exp(prediction[:,:,2:4])*anchors

    #Softmax the class scores
    prediction[:,:,5: 5 + num_classes] = torch.sigmoid((prediction[:,:, 5 : 5 + num_classes]))

    prediction[:,:,:4] *= stride

    return prediction

def load_classes(namesfile):
    fp = open(namesfile, "r")
    names = fp.read().split("\n")[:-1]
    return names

def get_im_dim(im):
    im = cv2.imread(im)
    w,h = im.shape[1], im.shape[0]
    return w,h

def unique(tensor):

    tensor_np = tensor.cpu().numpy()
    unique_np = np.unique(tensor_np)
    unique_tensor = torch.from_numpy(unique_np)
    
    tensor_res = tensor.new(unique_tensor.shape)
    tensor_res.copy_(unique_tensor)
    return tensor_res

def write_results(prediction, confidence, num_classes, nms = True, nms_conf = 0.4, CUDA=False):
    # conf_mask = (prediction[:, :, 4] > confidence).float().unsqueeze(2)
    # prediction = prediction*conf_mask
    #
    # box_a = prediction.new(prediction.shape)
    # box_a[:,:,0] = (prediction[:,:,0] - prediction[:,:,2]/2)
    # box_a[:,:,1] = (prediction[:,:,1] - prediction[:,:,3]/2)
    # box_a[:,:,2] = (prediction[:,:,0] + prediction[:,:,2]/2)
    # box_a[:,:,3] = (prediction[:,:,1] + prediction[:,:,3]/2)
    # prediction[:,:,:4] = box_a[:,:,:4]
    non_zero_ind = torch.nonzero(prediction[:, :, 4] > confidence)
    non_zero_prediction = prediction[non_zero_ind[:, 0], non_zero_ind[:, 1], :]

    box_a = non_zero_prediction.new(non_zero_prediction.shape)
    box_a[:, 0] = (non_zero_prediction[:, 0] - non_zero_prediction[:, 2]/2)
    box_a[:, 1] = (non_zero_prediction[:, 1] - non_zero_prediction[:, 3]/2)
    box_a[:, 2] = (non_zero_prediction[:, 0] + non_zero_prediction[:, 2]/2)
    box_a[:, 3] = (non_zero_prediction[:, 1] + non_zero_prediction[:, 3]/2)
    non_zero_prediction[:, :4] = box_a[:, :4]
    batch_size = prediction.size(0)

    output = prediction.new(1, prediction.size(2) + 1)
    write = False

    for i in range(batch_size):
        #select the image from the batch
        index = torch.nonzero(non_zero_ind[:, 0] == i).squeeze()
        image_pred = non_zero_prediction[index, :]
        
        #Get the class having maximum score, and the index of that class
        #Get rid of num_classes softmax scores 
        #Add the class index and the class score of class having maximum score
        max_conf, max_conf_score = torch.max(image_pred[:, 5:5 + num_classes], 1)
        max_conf = max_conf.float().unsqueeze(1)
        max_conf_score = max_conf_score.float().unsqueeze(1)
        seq = (image_pred[:,:5], max_conf, max_conf_score)
        image_pred_ = torch.cat(seq, 1)

        #Get rid of the zero entries
        # non_zero_ind =  (torch.nonzero(image_pred[:, 4]))
        # image_pred_ = image_pred[non_zero_ind.squeeze(), :].view(-1,7)

        #Get the various classes detected in the image
        try:
            img_classes = torch.unique(image_pred_[:, -1])
        except:
             continue
        #WE will do NMS classwise
        for cls in img_classes:
            #get the detections with one particular class
            #cls_mask = image_pred_*(image_pred_[:,-1] == cls).float().unsqueeze(1)
            #class_mask_ind = torch.nonzero(cls_mask[:,-2]).squeeze()
            class_mask_ind = torch.nonzero(image_pred_[:, -1] == cls).squeeze()
            image_pred_class = image_pred_[class_mask_ind, :]

            #sort the detections such that the entry with the maximum objectness
            #confidence is at the top
            conf_sort_index = torch.sort(image_pred_class[:,4], descending = True)[1]
            image_pred_class = image_pred_class[conf_sort_index]
            idx = image_pred_class.size(0)
            #if nms has to be done
            if nms:
                #For each detection
                for i in range(idx):
                    #Get the IOUs of all boxes that come after the one we are looking at 
                    #in the loop
                    try:
                        ious = bbox_iou(image_pred_class[i].unsqueeze(0), image_pred_class[i+1:], CUDA)
                    except ValueError:
                        break
                    except IndexError:
                        break
                    #Zero out all the detections that have IoU > treshhold
                    iou_mask = (ious < nms_conf).float().unsqueeze(1)
                    image_pred_class[i+1:] *= iou_mask       
                    
                    #Remove the non-zero entries
                    non_zero_ind = torch.nonzero(image_pred_class[:,4]).squeeze()
                    image_pred_class = image_pred_class[non_zero_ind].view(-1,7)

            #Concatenate the batch_id of the image to the detection
            #this helps us identify which image does the detection correspond to 
            #We use a linear straucture to hold ALL the detections from the batch
            #the batch_dim is flattened
            #batch is identified by extra batch column
            batch_ind = image_pred_class.new(image_pred_class.size(0), 1).fill_(i)
            seq = batch_ind, image_pred_class
            if not write:
                output = torch.cat(seq,1)
                write = True
            else:
                out = torch.cat(seq,1)
                output = torch.cat((output, out))
    
    return output

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sat Mar 24 00:12:16 2018

@author: ayooshmac
"""

def predict_transform_half(prediction, inp_dim, anchors, num_classes, CUDA = True):
    batch_size = prediction.size(0)
    stride =  inp_dim // prediction.size(2)

    bbox_attrs = 5 + num_classes
    num_anchors = len(anchors)
    grid_size = inp_dim // stride

    
    prediction = prediction.view(batch_size, bbox_attrs*num_anchors, grid_size*grid_size)
    prediction = prediction.transpose(1,2).contiguous()
    prediction = prediction.view(batch_size, grid_size*grid_size*num_anchors, bbox_attrs)
    
    
    #Sigmoid the  centre_X, centre_Y. and object confidencce
    prediction[:,:,0] = torch.sigmoid(prediction[:,:,0])
    prediction[:,:,1] = torch.sigmoid(prediction[:,:,1])
    prediction[:,:,4] = torch.sigmoid(prediction[:,:,4])

    
    #Add the center offsets
    grid_len = np.arange(grid_size)
    a,b = np.meshgrid(grid_len, grid_len)
    
    x_offset = torch.FloatTensor(a).view(-1,1)
    y_offset = torch.FloatTensor(b).view(-1,1)
    
    if CUDA:
        x_offset = x_offset.cuda().half()
        y_offset = y_offset.cuda().half()
    
    x_y_offset = torch.cat((x_offset, y_offset), 1).repeat(1,num_anchors).view(-1,2).unsqueeze(0)
    
    prediction[:,:,:2] += x_y_offset
      
    #log space transform height and the width
    anchors = torch.HalfTensor(anchors)
    
    if CUDA:
        anchors = anchors.cuda()
    
    anchors = anchors.repeat(grid_size*grid_size, 1).unsqueeze(0)
    prediction[:,:,2:4] = torch.exp(prediction[:,:,2:4])*anchors

    #Softmax the class scores
    prediction[:,:,5: 5 + num_classes] = nn.Softmax(-1)(Variable(prediction[:,:, 5 : 5 + num_classes])).data

    prediction[:,:,:4] *= stride
    
    
    return prediction


def write_results_half(prediction, confidence, num_classes, nms = True, nms_conf = 0.4):
    conf_mask = (prediction[:,:,4] > confidence).half().unsqueeze(2)
    prediction = prediction*conf_mask
    
    try:
        ind_nz = torch.nonzero(prediction[:,:,4]).transpose(0,1).contiguous()
    except:
        return 0
    
    
    
    box_a = prediction.new(prediction.shape)
    box_a[:,:,0] = (prediction[:,:,0] - prediction[:,:,2]/2)
    box_a[:,:,1] = (prediction[:,:,1] - prediction[:,:,3]/2)
    box_a[:,:,2] = (prediction[:,:,0] + prediction[:,:,2]/2) 
    box_a[:,:,3] = (prediction[:,:,1] + prediction[:,:,3]/2)
    prediction[:,:,:4] = box_a[:,:,:4]
    
    
    
    batch_size = prediction.size(0)
    
    output = prediction.new(1, prediction.size(2) + 1)
    write = False
    
    for ind in range(batch_size):
        #select the image from the batch
        image_pred = prediction[ind]

        
        #Get the class having maximum score, and the index of that class
        #Get rid of num_classes softmax scores 
        #Add the class index and the class score of class having maximum score
        max_conf, max_conf_score = torch.max(image_pred[:,5:5+ num_classes], 1)
        max_conf = max_conf.half().unsqueeze(1)
        max_conf_score = max_conf_score.half().unsqueeze(1)
        seq = (image_pred[:,:5], max_conf, max_conf_score)
        image_pred = torch.cat(seq, 1)
        
        
        #Get rid of the zero entries
        non_zero_ind =  (torch.nonzero(image_pred[:,4]))
        try:
            image_pred_ = image_pred[non_zero_ind.squeeze(),:]
        except:
            continue
        
        #Get the various classes detected in the image
        img_classes = unique(image_pred_[:,-1].long()).half()
        
        
        
                
        #WE will do NMS classwise
        for cls in img_classes:
            #get the detections with one particular class
            cls_mask = image_pred_*(image_pred_[:,-1] == cls).half().unsqueeze(1)
            class_mask_ind = torch.nonzero(cls_mask[:,-2]).squeeze()
            

            image_pred_class = image_pred_[class_mask_ind]

        
             #sort the detections such that the entry with the maximum objectness
             #confidence is at the top
            conf_sort_index = torch.sort(image_pred_class[:,4], descending = True )[1]
            image_pred_class = image_pred_class[conf_sort_index]
            idx = image_pred_class.size(0)
            
            #if nms has to be done
            if nms:
                #For each detection
                for i in range(idx):
                    #Get the IOUs of all boxes that come after the one we are looking at 
                    #in the loop
                    try:
                        ious = bbox_iou(image_pred_class[i].unsqueeze(0), image_pred_class[i+1:])
                    except ValueError:
                        break
        
                    except IndexError:
                        break
                    
                    #Zero out all the detections that have IoU > treshhold
                    iou_mask = (ious < nms_conf).half().unsqueeze(1)
                    image_pred_class[i+1:] *= iou_mask       
                    
                    #Remove the non-zero entries
                    non_zero_ind = torch.nonzero(image_pred_class[:,4]).squeeze()
                    image_pred_class = image_pred_class[non_zero_ind]
                    
                    
            
            #Concatenate the batch_id of the image to the detection
            #this helps us identify which image does the detection correspond to 
            #We use a linear straucture to hold ALL the detections from the batch
            #the batch_dim is flattened
            #batch is identified by extra batch column
            batch_ind = image_pred_class.new(image_pred_class.size(0), 1).fill_(ind)
            seq = batch_ind, image_pred_class
            
            if not write:
                output = torch.cat(seq,1)
                write = True
            else:
                out = torch.cat(seq,1)
                output = torch.cat((output,out))
    
    return output


def IOU(tl1, br1, tl2, br2):
    wh1, wh2 = br1 - tl1, br2 - tl2
    assert ((wh1 >= .0).all() and (wh2 >= .0).all())

    intersection_wh = np.maximum(np.minimum(br1, br2) - np.maximum(tl1, tl2), 0.)
    intersection_area = np.prod(intersection_wh)
    area1, area2 = (np.prod(wh1), np.prod(wh2))
    union_area = area1 + area2 - intersection_area;
    return intersection_area / union_area


def IOU_centre_and_dims(cc1, wh1, cc2, wh2):
    return IOU(cc1 - wh1 / 2., cc1 + wh1 / 2., cc2 - wh2 / 2., cc2 + wh2 / 2.)

def lpr_decode(img_wh, cars, lprs, inp_dim=(640,640)):
    mask = lprs[:, -1] > 0.3
    ind = torch.nonzero(mask)
    c_lprs = torch.zeros(cars.shape[0], 8, dtype=lprs.dtype).to(lprs.device)
    c_lprs[ind[:, 0], :] = lprs[ind[:, 0], 0:8] / (28 - 1)
    c_lprs = c_lprs.view(c_lprs.shape[0], 2, 4)
    c_lprs = c_lprs.permute(0, 2, 1).contiguous().view(-1, 8)
    #c_lprs[ind[:, 0], :] = 0

    c_lprs[:, [0, 2, 4, 6]] = (c_lprs[:, [0, 2, 4, 6]]) * (cars[:, 2] - cars[:, 0]).unsqueeze(1) + cars[:, 0].unsqueeze(1)
    c_lprs[:, [1, 3, 5, 7]] = (c_lprs[:, [1, 3, 5, 7]]) * (cars[:, 3] - cars[:, 1]).unsqueeze(1) + cars[:, 1].unsqueeze(1)

    #print(img_wh)
    #img_wh = torch.from_numpy(img_wh).to(detections_car.device)
    scaling_factor = np.min(inp_dim / img_wh)

    cars[:, [0, 2]] -= (inp_dim[0] - scaling_factor * img_wh[0]) / 2
    cars[:, [1, 3]] -= (inp_dim[1] - scaling_factor * img_wh[1]) / 2
    c_lprs[:, [0, 2, 4, 6]] -= (inp_dim[0] - scaling_factor * img_wh[0]) / 2
    c_lprs[:, [1, 3, 5, 7]] -= (inp_dim[1] - scaling_factor * img_wh[1]) / 2

    cars[:, 0:4] /= scaling_factor
    c_lprs[:, 0:8] /= scaling_factor

    cars[:, [0,2]] = torch.clamp(cars[:, [0,2]], 0.0, img_wh[0])
    cars[:, [1,3]] = torch.clamp(cars[:, [1,3]], 0.0, img_wh[1])
    c_lprs[:, [0, 2, 4, 6]] = torch.clamp(c_lprs[:, [0,2,4,6]], 0.0, img_wh[0])
    c_lprs[:, [1, 3, 5, 7]] = torch.clamp(c_lprs[:, [1,3,5,7]], 0.0, img_wh[1])

    return cars, c_lprs, mask

