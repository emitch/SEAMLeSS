import torch
import torch.nn as nn
import copy
from utilities.helpers import gridsample_residual, upsample, downsample, load_model_from_dict


class Model(nn.Module):
    """
    Defines an aligner network.
    This is the main class of the architecture code.

    `height` is the number of levels of the network
    `feature_maps` is the number of feature maps per encoding layer
    """

    def __init__(self, feature_maps=None, encodings=False, *args, **kwargs):
        super().__init__()
        self.feature_maps = feature_maps
        self.encode = EncodingPyramid(self.feature_maps) if encodings else None
        self.align = AligningPyramid(self.feature_maps if encodings
                                     else [1]*len(feature_maps))
        self.module = None

    def __getitem__(self, index):
        return self.submodule(index)

    def __len__(self):
        return self.height

    def forward(self, field, **kwargs):
        if self.module is None:
            self.module = self[0]
        src, tgt = -field[..., 0:1].permute(0, 3, 1, 2), -field[..., 1:2].permute(0, 3, 1, 2)
        return self.module(src, tgt, None)

    def load(self, path):
        """
        Loads saved weights into the model
        """
        with path.open('rb') as f:
            weights = torch.load(f)
        load_model_from_dict(self, weights)
        return self

    def save(self, path):
        """
        Saves the model weights to a file
        """
        with path.open('wb') as f:
            torch.save(self.state_dict(), f)

    @property
    def height(self):
        return len(self.feature_maps)

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
        return _SubmoduleView(self, index)


class Encoder(nn.Module):
    """
    Module that implements a two-convolution siamese encoder.
    These can be stacked to build an encoding pyramid.
    """

    def __init__(self, infm, outfm, k=3):
        super().__init__()
        p = (k-1)//2
        self.seq = nn.Sequential(
            nn.Conv2d(infm, outfm, k, padding=p),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(outfm, outfm, k, padding=p),
            nn.LeakyReLU(inplace=True),
        )
        self.seq.apply(init_leaky_relu)

    def forward(self, src, tgt):
        return self.seq(src), self.seq(tgt)


class EncodingPyramid(nn.Module):
    """
    A stack of siamese encoders with one Encoder module at each mip level.
    It takes a pair of images and returns a list of encodings, one for
    each element of `feature_list`

    `feature_list` should be a list of integers, each of which specifies
    the number of feature maps at a particular mip level.
    For example,
        >>> EncodingPyramid([2, 4, 8, 16])
    creates a pyramid with four Encoder modules, with 2, 4, 8, and 16
    feature maps respectively.
    `input_fm` is the number of input feature maps, and should remain 1
    for normal image inputs.
    """

    def __init__(self, feature_list, input_fm=1):
        super().__init__()
        self.feature_list = [input_fm] + list(feature_list)
        self.list = nn.ModuleList([
            Encoder(infm, outfm)
            for infm, outfm
            in zip(self.feature_list[:-1], self.feature_list[1:])
        ])

    def forward(self, src, tgt):
        src_encodings = []
        tgt_encodings = []
        for module in self.list:
            src, tgt = module(src, tgt)
            src_encodings.append(src)
            tgt_encodings.append(tgt)
            src, tgt = downsample()(src), downsample()(tgt)
        return src_encodings, tgt_encodings


class Aligner(nn.Module):
    """
    Module that takes a pair of images as input and outputs a vector field
    that can be used to transform one to the other.

    While the output of the module has the standard shape for input to
    the PyTorch gridsampler, the units of the field is pixels in order
    to be agnostic to the size of the input images.
    """

    def __init__(self, channels=1, k=7):
        super().__init__()
        p = (k-1)//2
        self.channels = channels
        self.seq = nn.Sequential(
            nn.Conv2d(channels * 2, 16, k, padding=p),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(16, 16, k, padding=p),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(16, 16, k, padding=p),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(16, 16, k, padding=p),
            nn.LeakyReLU(inplace=True),
            nn.Conv2d(16, 2, k, padding=p),
        )
        self.seq.apply(init_leaky_relu)

    def forward(self, src, tgt):
        if src.shape[1] != tgt.shape[1]:
            raise ValueError('Cannot align src and tgt of different shapes. '
                             'src: {}, tgt: {}'.format(src.shape, tgt.shape))
        elif src.shape[1] % self.channels != 0:
            raise ValueError('Number of channels does not divide stack size. '
                             '{} channels for {}'
                             .format(self.channels, src.shape))
        if src.shape[1] == self.channels:
            stack = torch.cat((src, tgt), dim=1)
            field = self.seq(stack).permute(0, 2, 3, 1)
            return field
        else:  # stack of encodings
            fields = []
            for pair in zip(src.split(self.channels, dim=1),
                            tgt.split(self.channels, dim=1)):
                stack = torch.cat(pair, dim=1)
                fields.append(self.seq(stack).permute(0, 2, 3, 1))
            return sum(fields) / len(fields)


class AligningPyramid(nn.Module):
    """
    A stack of Aligner modules with one Aligner at each mip level.
    It takes a pair of images and produces a vector field which maps from
    the first image, the source, to the second image, the target.

    If `src_input` and `tgt_input` are lists, then they are taken to be
    precomputed encodings or downsamples of the respective images.

    `feature_list` should be a list of integers, each of which specifies
    the number of feature maps at a particular mip level.
    For example,
        >>> AligningPyramid([2, 4, 8, 16])
    creates a pyramid with four Aligner modules, with 2, 4, 8, and 16
    feature maps respectively.
    """

    def __init__(self, feature_list):
        super().__init__()
        self.feature_list = list(feature_list)
        self.list = nn.ModuleList([Aligner(ch) for ch in feature_list])

    def forward(self, src_input, tgt_input, accum_field=None):
        for i in reversed(range(len(self.feature_list))):
            if isinstance(src_input, list) and isinstance(tgt_input, list):
                src, tgt = src_input[i], tgt_input[i]
            else:
                src, tgt = downsample(i)(src_input), downsample(i)(tgt_input)
            if accum_field is not None:
                accum_field = (upsample()(accum_field.permute(0, 3, 1, 2))
                               .permute(0, 2, 3, 1))
                src = gridsample_residual(src, accum_field,
                                          padding_mode='border')
            factor = 2 / src.shape[-1]  # scale to [-1,1]
            res_field = self.list[i](src, tgt) * factor
            if accum_field is not None:
                resampled = gridsample_residual(
                    accum_field.permute(0, 3, 1, 2), res_field,
                    padding_mode='border').permute(0, 2, 3, 1)
                accum_field = res_field + resampled
            else:
                accum_field = res_field
        return accum_field


class _SubmoduleView(nn.Module):
    """
    Returns a view into a sequence of aligners of a model.
    This is useful for training and testing.
    """

    def __init__(self, model, index):
        super().__init__()
        if isinstance(index, int):
            index = slice(index, index+1)
        self.levels = range(model.height)[index]
        self.encoders = model.encode.list if model.encode else None
        self.aligners = model.align.list[index]

    def forward(self, src, tgt, accum_field=None):
        # encode
        if self.encoders:
            src_stack, tgt_stack = [], []
            for module in self.encoders:
                src, tgt = module(src, tgt)
                src_stack.append(src)
                tgt_stack.append(tgt)
                src, tgt = downsample()(src), downsample()(tgt)
        else:
            src_stack, tgt_stack = src, tgt

        # align
        prev_level = None
        for i, aligner in zip(reversed(self.levels), reversed(self.aligners)):
            if isinstance(src_stack, list) and isinstance(tgt_stack, list):
                src, tgt = src_stack[i], tgt_stack[i]
            else:
                src, tgt = downsample(i)(src_stack), downsample(i)(tgt_stack)
            if prev_level is not None:
                accum_field = (upsample(prev_level - i)
                               (accum_field.permute(0, 3, 1, 2))
                               .permute(0, 2, 3, 1))
                # src = gridsample_residual(src, accum_field,
                #                           padding_mode='border')
            factor = 2 / src.shape[-1] * 100  # scale to [-1,1]
            res_field = aligner(src, tgt) * factor
            if accum_field is not None:
                resampled = gridsample_residual(
                    accum_field.permute(0, 3, 1, 2), res_field,
                    padding_mode='border').permute(0, 2, 3, 1)
                accum_field = res_field + resampled
            else:
                accum_field = res_field
            prev_level = i
        accum_field = (upsample(prev_level)
                       (accum_field.permute(0, 3, 1, 2))
                       .permute(0, 2, 3, 1))
        return accum_field

    def train_level(self, level=slice(None)):
        """
        Set only a specific level of the submodule to training mode and
        freeze all the other weights
        """
        for p in self.parameters():
            p.requires_grad = False
        for p in self.aligners[0].parameters():
            p.requires_grad = True
        if level == 'all':
            for p in self.parameters():
                p.requires_grad = True
        elif level == 'lowest':
            for p in self.aligners[0].parameters():
                p.requires_grad = True
        elif level == 'highest':
            for p in self.aligners[-1].parameters():
                p.requires_grad = True
        else:
            for p in self.aligners[level].parameters():
                p.requires_grad = True
        return self

    def init_level(self, level='lowest'):
        """
        Initialize the last level of the SubmoduleView by copying the trained
        weights of the next to last level.
        Whether the last level is the lowest or highest level is determined
        by the `level` argument.
        If the SubmoduleView has only one level, this does nothing.
        """
        # TODO: init encoders, handle different size aligners
        if len(self.aligners) > 1:
            if level == 'lowest':
                state_dict = self.aligners[1].state_dict()
                self.aligners[0].load_state_dict(state_dict)
            elif level == 'highest':
                state_dict = self.aligners[-2].state_dict()
                self.aligners[-1].load_state_dict(state_dict)
        return self

    @property
    def pixel_size_ratio(self):
        """
        The ratio of the pixel size of the submodule's highest level to
        the pixel size at its input level.
        By assumption, each level of the network has equal ability, so this
        is a measure of the power of the submodule to detect and correct
        large misalignments in its input scale.
        """
        return 2**(self.levels[-1])


def init_leaky_relu(m, a=None):
    """
    Initialize to account for the default negative slope of LeakyReLU.
    PyTorch's LeakyReLU by defualt has a slope of 0.01 for negative
    values, but the default initialization for Conv2d uses
    `kaiming_uniform_` with `a=math.sqrt(5)`. (ref: https://goo.gl/Bx3wdS)
    Instead, this initializes according to He, K. et al. (2015).
    (ref https://goo.gl/hH6qaM)

    If `a` is given it uses that as the negative slope. If it is None,
    the default for LeakyReLU is used.
    """
    if not isinstance(m, torch.nn.Conv2d):
        return
    if a is None:
        a = nn.modules.activation.LeakyReLU().negative_slope
    nn.init.kaiming_uniform_(m.weight, a=a)


# helper functions kept around temporarily... TODO: remove

def copy_aligner(self, id_from, id_to):
    """
    Copy the kernel weights from one aligner module to another
    """
    if min(id_from, id_to) < 0 or max(id_from, id_to) >= self.height:
        raise IndexError('Values {} --> {} out of bounds for size {}.'
                         .format(id_from, id_to, self.height))
    state_dict = self.align.list[id_from].state_dict()
    self.align.list[id_to].load_state_dict(state_dict)


def shift_aligners(self):
    """
    Shift the kernel weights up one aligner and make a copy of the lowest
    """
    for i in range(self.height-1, 1, -1):
        self.align.list[i] = self.align.list[i-1]
    self.align.list[1] = copy.deepcopy(self.align.list[0])


def copy_encoder(self, id_from, id_to):
    """
    Copy the kernel weights from one encoder module to another
    """
    if min(id_from, id_to) < 0 or max(id_from, id_to) >= self.height:
        raise IndexError('Values {} --> {} out of bounds for size {}.'
                         .format(id_from, id_to, self.height))
    state_dict = self.encode.list[id_from].state_dict()
    self.encode.list[id_to].load_state_dict(state_dict)
