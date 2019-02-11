import torch
import torch.nn as nn
from torch.autograd import Variable
from utilities.helpers import save_chunk
import numpy as np

def lap(fields):
    def dx(f):
        p = Variable(torch.zeros((f.size(0),1,f.size(1),2), device='cuda'))
        return torch.cat((p, f[:,1:-1,:,:] - f[:,:-2,:,:], p), 1)
    def dy(f):
        p = Variable(torch.zeros((f.size(0),f.size(1),1,2), device='cuda'))
        return torch.cat((p, f[:,:,1:-1,:] - f[:,:,:-2,:], p), 2)
    def dxf(f):
        p = Variable(torch.zeros((f.size(0),1,f.size(1),2), device='cuda'))
        return torch.cat((p, f[:,1:-1,:,:] - f[:,2:,:,:], p), 1)
    def dyf(f):
        p = Variable(torch.zeros((f.size(0),f.size(1),1,2), device='cuda'))
        return torch.cat((p, f[:,:,1:-1,:] - f[:,:,2:,:], p), 2)
    fields = map(lambda f: [dx(f), dy(f), dxf(f), dyf(f)], fields)
    fields = map(lambda fl: (sum(fl) / 4.0) ** 2, fields)
    field = sum(map(lambda f: torch.sum(f, -1), fields))
    return field

def jacob(fields):
    def dx(f):
        p = Variable(torch.zeros((f.size(0),1,f.size(1),2), device='cuda'))
        return torch.cat((p, f[:,2:,:,:] - f[:,:-2,:,:], p), 1)
    def dy(f):
        p = Variable(torch.zeros((f.size(0),f.size(1),1,2), device='cuda'))
        return torch.cat((p, f[:,:,2:,:] - f[:,:,:-2,:], p), 2)
    fields = sum(map(lambda f: [dx(f), dy(f)], fields), [])
    field = torch.sum(torch.cat(fields, -1) ** 2, -1)
    return field

def cjacob(fields):
    def center(f):
        fmean_x, fmean_y = torch.mean(f[:,:,:,0]).item(), torch.mean(f[:,:,:,1]).item()
        fmean = torch.cat((fmean_x * torch.ones((1,f.size(1), f.size(2),1), device='cuda'), fmean_y * torch.ones((1,f.size(1), f.size(2),1), device='cuda')), 3)
        fmean = Variable(fmean).cuda()
        return f - fmean

    def dx(f):
        p = Variable(torch.zeros((f.size(0),1,f.size(1),2), device='cuda'))
        d = torch.cat((p, f[:,2:,:,:] - f[:,:-2,:,:], p), 1)
        return center(d)
    def dy(f):
        p = Variable(torch.zeros((f.size(0),f.size(1),1,2), device='cuda'))
        d = torch.cat((p, f[:,:,2:,:] - f[:,:,:-2,:], p), 2)
        return center(d)

    fields = sum(map(lambda f: [dx(f), dy(f)], fields), [])
    field = torch.sum(torch.cat(fields, -1) ** 2, -1)
    return field

def tv(fields):
    def dx(f):
        p = Variable(torch.zeros((f.size(0),1,f.size(1),2), device='cuda'))
        return torch.cat((p, f[:,2:,:,:] - f[:,:-2,:,:], p), 1)
    def dy(f):
        p = Variable(torch.zeros((f.size(0),f.size(1),1,2), device='cuda'))
        return torch.cat((p, f[:,:,2:,:] - f[:,:,:-2,:], p), 2)
    fields = sum(map(lambda f: [dx(f), dy(f)], fields), [])
    field = torch.sum(torch.abs(torch.cat(fields, -1)), -1)
    return field

def smoothness_penalty(ptype):
    def penalty(fields, weights=None):
        if ptype ==     'lap': field = lap(fields)
        elif ptype == 'jacob': field = jacob(fields)
        elif ptype == 'cjacob': field = cjacob(fields)
        elif ptype ==    'tv': field = tv(fields)
        else: raise ValueError("Invalid penalty type: {}".format(ptype))

        if weights is not None:
            field = field * weights
        return field
    return penalty

def similarity_score(should_reduce=False):
    return lambda x, y: torch.mean((x-y)**2) if should_reduce else (x-y)**2
