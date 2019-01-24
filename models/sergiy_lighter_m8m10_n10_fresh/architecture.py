import torch
import torch.nn as nn
import copy
from utilities.helpers import gridsample_residual, upsample, downsample, load_model_from_dict
from .alignermodule import Aligner
from .rollback_pyramid import RollbackPyramid
from .masker import Masker

class Model(nn.Module):
    """
    Defines an aligner network.
    This is the main class of the architecture code.

    `height` is the number of levels of the network
    `feature_maps` is the number of feature maps per encoding layer
    """

    def __init__(self, height=3, mips=(8, 10), *args, **kwargs):
        super().__init__()
        self.height = height
        self.mips = mips
        self.align = RollbackPyramid()
        self.aligndict = {}
        self.lighter = None
        self.lighter_mip = 9
        self.downsampler = torch.nn.AvgPool2d((2, 2))
        self.upsampler = torch.nn.functional.interpolate

    def __getitem__(self, index):
        return self.submodule(index)

    def forward(self, src, tgt, in_field=None, plastic_mask=None, mip_in=6,
                **kwargs):
        src_lighter_in = src - 0.5 #argh
        tgt_lighter_in = tgt - 0.5
        for _ in range(mip_in, self.lighter_mip):
            src_lighter_in = self.downsampler(src_lighter_in)
            tgt_lighter_in = self.downsampler(tgt_lighter_in)

        src_light = self.lighter(src_lighter_in)
        tgt_light = self.lighter(tgt_lighter_in)
        for _ in range(mip_in, self.lighter_mip):
            src_light = self.upsampler(src_light, scale_factor=2)
            tgt_light = self.upsampler(tgt_light, scale_factor=2)

        src_final = src + src_light
        tgt_final = tgt + tgt_light
        stack = torch.cat((src_final, tgt_final), 1)
        # stack_t = stack.transpose(2, 3)
        # field_t = self.align(stack_t, plastic_mask=None, mip_in=mip_in)
        # field_t = field_t * 2 / src.shape[-2]
        # field = field_t.transpose(1, 2).flip(3)
        field = self.align(stack, plastic_mask=None, mip_in=mip_in)
        field = field * 2 / src.shape[-2]
        # field = field.transpose(1, 2)
        return field

    def load(self, path):
        """
        Loads saved weights into the model
        """
        for m in self.mips:
            fms = 24
            self.aligndict[m] = Aligner(fms=[2, fms, fms, fms, fms, 2], k=7).cuda()
            with (path/'12_21_n10_fresh_s1007_mip_10_8_module{}.pth.tar'.format(m)).open('rb') as f:
                self.aligndict[m].load_state_dict(torch.load(f))
            self.align.set_mip_processor(self.aligndict[m], m)
        self.lighter = Masker(fms=[1, fms, fms, fms, fms, fms, 1], k=7).cuda()
        with (path/'01_16_lighter_module9.pth.tar').open('rb') as f:
            self.lighter.load_state_dict(torch.load(f))
        return self

    def save(self, path):
        """
        Saves the model weights to a file
        """
        raise NotImplementedError()
        # with path.open('wb') as f:
        #     torch.save(self.state_dict(), f)

    def submodule(self, index):
        """
        Returns a submodule as indexed by `index`.

        Submodules with lower indecies are intended to be trained earlier,
        so this also decides the training order.

        `index` must be an int, a slice, or None.
        If `index` is a slice, the submodule contains the relevant levels.
        If `index` is None or greater than the height, the submodule
        returned contains the whole model.
        """
        if index is None or (isinstance(index, int)
                             and index >= self.height):
            index = slice(self.height)
        if isinstance(index, int):
            index = slice(index, index+1)
        newmips = range(max(self.aligndict.keys()))[index]
        sub = Model(height=self.height, mips=newmips)
        for m in newmips:
            sub.align.set_mip_processor(self.aligndict[m], m)
        return sub
