import random
import h5py
import cv2
import torch
from torch.utils.data import Dataset, ConcatDataset

from aug import aug_input, rotate_and_scale, random_translation


def compile_dataset(*h5_paths, transform=None):
    datasets = []
    for h5_path in h5_paths:
        h5f = h5py.File(h5_path, 'r')
        ds = [StackDataset(v, transform=transform) for v in h5f.values()]
        datasets.extend(ds)
    return ConcatDataset(datasets)


class RandomAugmentation(object):
    """Apply random Gaussian noise, cutouts, & brightness adjustment
    """

    def __init__(self, factor=2):
        self.factor = factor

    def __call__(self, X):
        src, tgt = X['src'].clone(), X['tgt'].clone()
        aug_src, aug_src_masks = aug_input(src)
        aug_tgt, aug_tgt_masks = aug_input(tgt)
        X['aug_src'] = aug_src
        X['aug_tgt'] = aug_tgt
        X['aug_src_masks'] = aug_src_masks
        X['aug_tgt_masks'] = aug_src_masks
        return X


class RandomFlip(object):
    """Randomly flip src & tgt images
    """

    def __call__(self, X):
        if random.choice([True, False]):
            X = X.flip(1)
        if random.choice([True, False]):
            X = X.flip(2)
        return X


class RandomTranslation(object):
    """Randomly translate src & tgt images separately
    """

    def __init__(self, max_displacement=2**6):
        self.max_displacement = max_displacement

    def __call__(self, X):
        src, tgt = X['src'], X['tgt']
        if random.randint(0, 1) == 0:
            src = random_translation(src, self.max_displacement)
        if random.randint(0, 1) == 0:
            tgt = random_translation(tgt, self.max_displacement)
        return {'src': src, 'tgt': tgt}


class RandomRotateAndScale(object):
    """Randomly rotate & scale src and tgt images
    """

    def __call__(self, X):
        if random.choice([True, False]):
            X, _ = rotate_and_scale(X, None)
        return X.squeeze()


class Contrast(object):
    """Contrast Limited Adaptive Histogram Equalization
    """

    def __init__(self, clipLimit=40, tileGridSize=(8, 8)):
        self.clahe = cv2.createCLAHE(clipLimit=clipLimit,
                                     tileGridSize=tileGridSize)

    def __call__(self, X):
        Xb = (X * 255).to(torch.uint8)
        for i in range(Xb.shape[-3]):
            eq = self.clahe.apply(Xb[..., i, :, :].squeeze().numpy())
            X[..., i, :, :] = torch.from_numpy(eq).unsqueeze(0)
        X = X.to(torch.float) / 255
        X[Xb == 0] = 0
        return X


class ToFloatTensor(object):
    """Convert ndarray to FloatTensor
    """

    def __call__(self, X):
        return torch.from_numpy(X).to(torch.float)


class Normalize(object):
    """Rescale values from 0 to 1
    """

    def __call__(self, X):
        X = X - X.min()
        X = X / X.max()
        return X


class Split(object):
    """Split sample into a (src, tgt) pair
    """

    def __call__(self, X):
        return {'src': X[0:1, ...], 'tgt': X[1:2, ...]}


class StackDataset(Dataset):
    """Deliver consecutive image pairs from 3D image stack

    Args:
        stack (4D ndarray): 1xZxHxW image array
    """

    def __init__(self, stack, transform=None):
        self.stack = stack
        self.N = self.stack.shape[1]-1
        self.transform = transform

    def __len__(self):
        # 2*(stack.shape[1]-1) consecutive image pairs
        return 2*self.N

    def __getitem__(self, k):
        # match i -> i+1 if k < stack.shape[1], else match i -> i-1
        i = self.N - abs(k - self.N)
        j = self.N - abs(k+1 - self.N)
        i, j = sorted((i, j))
        X = self.stack[0, i:j+1, :, :]
        if self.transform:
            X = self.transform(X)
        return X
