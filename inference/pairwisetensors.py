import os
from time import time

import torch
import numpy as np

from boundingbox import BoundingBox, BoundingCube
from cloudtensor import MiplessCloudTensor, MiplessCloudField
from cloudsample import cloudsample_multicompose

from taskqueue import RegisteredTask

class PairwiseTensors():

    def __init__(self, path, offsets, bbox, mip, pad, device='cpu', **kwargs):
        """Manage set of CloudTensors that contain objects specified by pair of neighbors
        
        Object examples include pairwise fields and images related to pairwise fields, 
        such as confidence maps.
        
        Args:
            path: str to directory with pairwise object format
                ./{OFFSETS}
                f_{z+offset \leftarrow z} is stored in OFFSET[Z]
            offsets: list of ints indicating offset (the distance from source to
                targets).
            bbox: BoundingBox
            mip: int for MIP level of fields
            pad: int for amount of padding to use in composing fields
            kwargs: will be passed to MiplessCloudTensor
        """
        self.offsets = offsets
        self.bbox = bbox
        self.mip = mip
        self.pad = pad
        self.cvs = {}
        for o in offsets:
            cv_path = os.path.join(path, str(o))
            self.cvs[o] = self.cloudtype(cv_path, device=device, **kwargs)

    @property
    def cloudtype(self):
        return MiplessCloudTensor

    @property
    def info(self):
        """Get info file of first MiplessTensor in self.cvs. This info file
        should be identical across all MiplessTensors for this object.
        """
        return self.cvs.get(list(self.cvs.keys())[0]).info(mip=self.mip)

    def mkdir(self):
        for cv in self.cvs.values():
            cv.mkdir()

    def exists(self):
        raise NotImplementedError

    def __setitem__(self, tgt_to_src, field):
        """Save pairwise object at ./{OFFSET}[:,:,z]
        """
        if len(tgt_to_src) != 2:
            raise ValueError('len(tgt_to_src) is {} != 2. '
                             'Pairwise objects are only defined between '
                             'a pair of sections.'.format(len(tgt_to_src)))
        tgt, src = tgt_to_src
        offset = tgt - src
        cv = self.cvs[offset][self.mip]
        bcube = BoundingCube.from_bbox(self.bbox, zs=src)
        cv[bcube] = field

    def __getitem__(self, tgt_to_src):
        """Get pairwise object at ./{OFFSET}[:,:,z] 

        Args:
            tgt_to_src: pair of ints as (target, source)
        """
        if len(tgt_to_src) != 2:
            raise ValueError('len(tgt_to_src) is {} != 2. '
                             'Pairwise objects are only defined between '
                             'a pair of sections.'.format(len(tgt_to_src)))
        tgt, src = tgt_to_src
        offset = tgt - src
        if offset not in self.offsets:
            raise ValueError('Requested offset {} is unavailable'.format(offset))
        cv = self.cvs[offset][self.mip]
        bcube = BoundingCube.from_bbox(self.bbox, zs=src)
        return cv[bcube]
    

class PairwiseFields(PairwiseTensors):
    """Manage set of CloudFields that contain fields between neighbors
    
    We use the following notation to define a pairwise field which aligns
    section z to section z+k.
    
        $f_{z+k \leftarrow z}$
    
    It's easiest to interpret this as the displacement field which warps
    section z to look like section z+k. 
    
    We define the offset as the distance between the source and the target. So
    in the case above, the offset is k.
    
    We store this field in a CloudTensor where the path indicates the offset,
    and the actual field will be stored at cv[..., z].
    
    One purpose of this class is to easily compose pairwise fields together.
    For example, if we wanted to create the field:
    
        $f_{z+k \leftarrow z_j} \circ f_{z+j \leftarrow z}$ 
    
    Then we can access it with the convention:
    
        ```
        F = PairwiseFields(path, offsets, bbox, mip)
        f = F[(z+k, z+j, z)]
        ```
    
    Args:
        path: str to directory with pairwise field format
            ./{OFFSETS}
            f_{z+offset \leftarrow z} is stored in OFFSET[Z]
        offsets: list of ints indicating offset (the distance from source to
            targets).
        bbox: BoundingBox
        mip: int for MIP level of fields
        pad: int for amount of padding to use in composing fields
        device: str for torch.device
        kwargs: will be passed to MiplessCloudTensor
    """
    @property
    def cloudtype(self):
        return MiplessCloudField

    def __getitem__(self, tgt_to_src):
        """Get field created by composing fields accessed by z_list[::-1]

        Args:
            tgt_to_src: list of ints, sorted from target to source, e.g.
                f_{0 \leftarrow 2} \circ f_{2 \leftarrow 3} : [0, 2, 3]
        """
        if len(tgt_to_src) <= 2:
            return super().__getitem__(tgt_to_src)
        else:
            offsets = np.array([t-s for t,s in zip(tgt_to_src[:-1], tgt_to_src[1:])])
            unavailable = any([o not in self.offsets for o in offsets])
            if unavailable:
                raise ValueError('Requested offsets {} are '
                                 'unavailable'.format(offsets[unavailable]))
            cvs = [self.cvs[o] for o in offsets]
            return cloudsample_multicompose(field_list=cvs, 
                                            z_list=tgt_to_src[1:], 
                                            bbox=self.bbox, 
                                            mip_list=[self.mip]*len(cvs),
                                            dst_mip=self.mip,
                                            factors=None,
                                            pad=self.pad)
        

class PairwiseVoteTask(RegisteredTask):
    def __init__(self, estimates_path, corrected_path, weights_path, 
                        offsets, src_z, tgt_offsets, bbox, mip, pad,
                        device, softmin_temp, blur_sigma):
        """Correct all pairwise fields from src_z to src_z+tgt_offsets

        Args:
            estimates_path: path to directory with pairwise fields to
                be corrected.
            corrected_path: path to directory where corrected pairwise
                field will be stored
            weights_path: path to directory where weights of corrected
                pairwise field will be stored
            offsets: list of ints indicating which offset subdirectories
                are available for the PairwiseField to use
            src_z: int for z index of section which will always be the 
                source in all pairwise fields
            tgt_offsets: list of ints indicating set of offsets to
                be used as tgts for pairwise fields; must be subset of
                `offsets`.
            bbox: serialized BoundingBox
            mip: int for MIP level of all fields
            pad: int used to uncrop field for profiling ahead of composition
            device: str indicating device for torch.Tensors
            softmine_temp: float for temperature of voting's softmin
            blur_sigma: float for std of spatial Gaussian used before voting
        """
        super().__init__(estimates_path, corrected_path, weights_path,
                        offsets, src_z, tgt_offsets, bbox, mip, pad,
                        device, softmin_temp, blur_sigma)

    def execute(self):
        z = self.src_z
        bbox = BoundingBox.deserialize(self.bbox)
        estimated_fields = PairwiseFields(path=self.estimates_path, 
                                         offsets=self.offsets,
                                         bbox=bbox,
                                         mip=self.mip,
                                         pad=self.pad,
                                         device=self.device,
                                         fill_missing=True)
        corrected_fields = PairwiseFields(self.corrected_path, 
                                         offsets=self.offsets,
                                         bbox=bbox,
                                         mip=self.mip,
                                         pad=self.pad,
                                         device=self.device,
                                         fill_missing=True)
        corrected_weights = PairwiseTensors(self.weights_path, 
                                         offsets=self.offsets,
                                         bbox=bbox,
                                         mip=self.mip,
                                         pad=self.pad,
                                         fill_missing=True)

        print("\nFieldsVote\n"
                "estimated_fields {}\n"
                "corrected_fields {}\n"
                "weights {}\n"
                "src_z {}\n"
                "tgt_offsets {}\n"
                "MIP{}\n"
                "softmin_temp {}\n"
                "blur_sigma {}\n".format(self.estimates_path, 
                                    self.corrected_path, 
                                    self.weights_path,
                                    self.src_z, 
                                    self.tgt_offsets, 
                                    self.mip, 
                                    self.softmin_temp,
                                    self.blur_sigma), flush=True)
        start = time()
        offsets = np.array(self.tgt_offsets)
        for k in offsets:
            # 3-way vector voting to nearest sections, with randomization to break tie
            estimates = [estimated_fields[(z+k, z)]]
            random_offsets = np.random.permutation(offsets)
            intermediaries = random_offsets[np.argsort(np.abs(random_offsets - k))[1:3]]
            print('Voting for F[{}]'.format((z+k, z)))
            for j in intermediaries:
                print('Using fields F[{}]'.format((z+k, z+j, z)))
                # f_{z+k \leftarrow z+j} \circ f_{z+j \leftarrow z}
                f = estimated_fields[(z+k, z+j, z)]
                estimates.append(f)

            estimates = torch.cat([f.field for f in estimates]).field()
            weights = estimates.voting_weights(softmin_temp=self.softmin_temp,
                                                  blur_sigma=self.blur_sigma)
            partition = weights.sum(dim=0, keepdim=True)
            weights = weights / partition
            field = (estimates * weights.unsqueeze(-3)).sum(dim=0, keepdim=True)
            corrected_fields[(z+k, z)] = field
            corrected_weights[(z+k, z)] = partition.unsqueeze(0) 

        end = time()
        diff = end - start
        print('FieldsVoteTask: {:.3f} s'.format(diff))

# class PairwiseComputeField:
#     raise NotImplementedError
