import torch
import numpy as np
from model.PyramidTransformer import PyramidTransformer
from utilities.archive import ModelArchive
from model.xmas import Xmas
from normalizer import Normalizer
from utilities.helpers import save_chunk

class Process(object):
    """docstring for Process."""
    def __init__(self, archive, mip, dim=1280, size=7, flip_average=True):
        super(Process, self).__init__()
        self.height = size
        self.archive = archive
        self.model = self.archive.model
        self.mip = mip
        self.dim = dim
        self.flip_average = flip_average

    @torch.no_grad()
    def process(self, s, t, level=0, crop=0, old_vectors=False):
        """Run source & target image through SEAMLeSS net. Provide final
        vector field and intermediaries.

        Args:
           s: source tensor
           t: target tensor
           level: MIP of source & target images
           crop: one-sided pixel amount to crop from final vector field
           old_vectors: flag to use vector handling from previous versions of torch 

        If flip averaging is on, run the net twice.
        The second time, flip the image 180 degrees.
        Then average the resulting (unflipped) vector fields.
        This eliminates the effect of any gradual drift.
        """
        if level != self.mip:
            return None

        # nonflipped
        unflipped, residuals, encodings, cumulative_residuals = self.model(s, t, old_vectors=old_vectors), *[None]*3
        unflipped *= (unflipped.shape[-2] / 2) * (2 ** self.mip)
        if crop>0:
            unflipped = unflipped[:,crop:-crop, crop:-crop,:]

        if not self.flip_average:
            return unflipped, residuals, encodings, cumulative_residuals

        # flipped
        s = s.flip([2, 3])
        t = t.flip([2, 3])
        field_fl, residuals_fl, encodings_fl, cumulative_residuals_fl = self.model(s, t, old_vectors=old_vectors), *[None]*3
        field_fl *= (field_fl.shape[-2] / 2) * (2 ** self.mip)
        if crop>0:
            field_fl = field_fl[:,crop:-crop, crop:-crop,:]
        flipped = -field_fl.flip([1,2])
        
        return (flipped + unflipped)/2.0, residuals, encodings, cumulative_residuals # TODO: include flipped resid & enc
#        return flipped, residuals_fl, encodings_fl, cumulative_residuals_fl # TODO: include flipped resid & enc

#Simple test
if __name__ == "__main__":
    print('Testing...')
    a = Process()
    s = np.ones((2,256,256), dtype=np.float32)
    t = np.ones((2,256,256), dtype=np.float32)

    flow = a.process(s, t, level=7)
    assert flow.shape == (2,256,256,2)

    flow = a.process(s, t, level=8, crop=10)
    assert flow.shape == (2,236,236,2)

    flow = a.process(s, t, level=11)
    assert flow == None

    flow = a.process(s, t, level=1, crop=10)
    assert flow == None

    print ('All tests passed.')
