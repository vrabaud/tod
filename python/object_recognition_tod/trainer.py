#!/usr/bin/env python
"""
Module defining the TOD trainer to train the TOD models
"""

from ecto import BlackBoxCellInfo as CellInfo, BlackBoxForward as Forward
from ecto_opencv import calib, features2d, highgui
from ecto_opencv.features2d import FeatureDescriptor
from object_recognition_core.db.cells import ModelWriter
from object_recognition_core.pipelines.training import TrainerBase, ObservationDealer
from object_recognition_tod import ecto_training
import ecto
from ecto import BlackBoxCellInfo as CellInfo, BlackBoxForward as Forward

########################################################################################################################

class TodTrainer(ecto.BlackBox, TrainerBase):
    def __init__(self, *args, **kwargs):
        ecto.BlackBox.__init__(self, *args, **kwargs)
        TrainerBase.__init__(self)

    @classmethod
    def declare_cells(cls, _p):
        return {'model_writer': CellInfo(ModelWriter),
                'observation_dealer': CellInfo(ObservationDealer),
                'passthrough': ecto.PassthroughN(items={'json_db':'The DB parameters as a JSON string'})}

    @classmethod
    def declare_forwards(cls, _p):
        p = {'model_writer': [Forward('json_submethod')],
             'passthrough': 'all'}
        i = {}
        o = {}

        return (p,i,o)

    def configure(self, p, i, o):
        self.incremental_model_builder = TodIncrementalModelBuilder()
        self.post_processor = TodPostProcessor()

    def connections(self, p):
        connections = []
        # Connect the model builder to the source
        for key in self.observation_dealer.outputs.iterkeys():
            if key in  self.incremental_model_builder.inputs.keys():
                connections += [self.observation_dealer[key] >> self.incremental_model_builder[key]]

        # connect the output to the post-processor
        for key in set(self.incremental_model_builder.outputs.keys()).intersection(self.post_processor.inputs.keys()):
            connections += [self.incremental_model_builder[key] >> self.post_processor[key]]

        # and write everything to the DB
        connections += [self.post_processor["db_document"] >> self.model_writer["db_document"]]

        return connections

########################################################################################################################

class TodIncrementalModelBuilder(ecto.BlackBox):
    """
    Given a set of observations, this BlackBox updates the current model
    """
    @classmethod
    def declare_cells(cls, _p):
        return {'feature_descriptor': CellInfo(FeatureDescriptor),
                'source': ecto.PassthroughN(items=dict(image='An image',
                                                   depth='A depth image',
                                                   mask='A mask for valid object pixels.',
                                                   K='The camera matrix',
                                                   R='The rotation matrix',
                                                   T='The translation vector',
                                                   frame_number='The frame number.'
                                                   )
                                        ),
                'model_stacker': ecto_training.TodModelStacker()
               }

    @staticmethod
    def declare_direct_params(p):
        p.declare('visualize', 'If true, displays images at runtime', False)

    def declare_forwards(self, p):
        p = {'feature_descriptor': [Forward('json_feature_params'), Forward('json_descriptor_params')]}
        i = {'source': 'all'}
        o = {'model_stacker': 'all'}

        return (p, i, o)

    def configure(self, p, i, o):
        self.depth_to_3d_sparse = calib.DepthTo3dSparse()
        self.keypoints_to_mat = features2d.KeypointsToMat()
        self.camera_to_world = ecto_training.CameraToWorld()
        from ecto_image_pipeline.base import RescaledRegisteredDepth
        self.rescale_depth = RescaledRegisteredDepth()  # this is for SXGA mode scale handling.
        self.keypoint_validator = ecto_training.KeypointsValidator()
        self.visualize = p.visualize

    def connections(self, p):
        graph = []
        # connect the input
        if 'depth' in self.feature_descriptor.inputs.keys():
            graph += [self.rescale_depth['depth'] >> self.feature_descriptor['depth']]
        graph += [self.source['image'] >> self.feature_descriptor['image'],
                           self.source['image'] >> self.rescale_depth['image'],
                           self.source['mask'] >> self.feature_descriptor['mask'],
                           self.source['depth'] >> self.rescale_depth['depth'],
                           self.source['K'] >> self.depth_to_3d_sparse['K']]

        # Make sure the keypoints are in the mask and with a valid depth
        graph += [self.feature_descriptor['keypoints', 'descriptors'] >> 
                            self.keypoint_validator['keypoints', 'descriptors'],
                            self.source['K'] >> self.keypoint_validator['K'],
                            self.source['mask'] >> self.keypoint_validator['mask'],
                            self.rescale_depth['depth'] >> self.keypoint_validator['depth'] ]

        # transform the keypoints/depth into 3d points
        graph += [ self.keypoint_validator['points'] >> self.depth_to_3d_sparse['points'],
                        self.rescale_depth['depth'] >> self.depth_to_3d_sparse['depth'],
                        self.source['R'] >> self.camera_to_world['R'],
                        self.source['T'] >> self.camera_to_world['T'],
                        self.depth_to_3d_sparse['points3d'] >> self.camera_to_world['points']]

        # store all the info
        graph += [ self.camera_to_world['points'] >> self.model_stacker['points3d'],
                        self.keypoint_validator['points', 'descriptors', 'disparities'] >> 
                                                            self.model_stacker['points', 'descriptors', 'disparities'],
                        self.source['K', 'R', 'T'] >> self.model_stacker['K', 'R', 'T'],
                        ]

        if self.visualize:
            mask_view = highgui.imshow(name="mask")
            depth_view = highgui.imshow(name="depth")

            graph += [ self.source['mask'] >> mask_view['image'],
                       self.source['depth'] >> depth_view['image']]
            # draw the keypoints
            keypoints_view = highgui.imshow(name="Keypoints")
            draw_keypoints = features2d.DrawKeypoints()
            graph += [ self.source['image'] >> draw_keypoints['image'],
                       self.feature_descriptor['keypoints'] >> draw_keypoints['keypoints'],
                       draw_keypoints['image'] >> keypoints_view['image']]

        return graph

########################################################################################################################

class TodPostProcessor(ecto.BlackBox):
    """
    Once the different features are computed, the points are merged together and a mesh is computed
    """
    @classmethod
    def declare_cells(cls, _p):
        return {'model_filler': ecto_training.ModelFiller(),
                'source': ecto.PassthroughN(items=dict(K='The camera matrix',
                                            quaternions='A vector of quaternions',
                                            Ts='A vector of translation vectors',
                                            disparities='The disparities of the measurements',
                                            descriptors='A stacked vector of descriptors',
                                            points='The 2D measurements per point.',
                                            points3d='The estimated 3d position of the points (3-channel matrices).'
                                           )
                                        )}

    @classmethod
    def declare_forwards(cls, _p):
        return ({}, {'source': 'all'}, {'model_filler': 'all'})

    def configure(self, p, i, o):
        self.point_merger = ecto_training.PointMerger()

    def connections(self, p):
        return [ self.source['descriptors'] >> self.point_merger['descriptors'],
                 self.source['points3d'] >> self.point_merger['points'],
                 self.point_merger['points', 'descriptors'] >> self.model_filler['points', 'descriptors']
                 ]
