#!/usr/bin/env python
"""
Module defining the TOD detector to find objects in a scene
"""

from ecto import BlackBoxCellInfo as CellInfo, BlackBoxForward as Forward
from ecto_image_pipeline.base import RescaledRegisteredDepth
from ecto_opencv import features2d, highgui, imgproc, calib
from ecto_opencv.calib import DepthTo3d
from ecto_opencv.features2d import FeatureDescriptor
from object_recognition_core.pipelines.detection import DetectorBase
from object_recognition_tod import ecto_detection
import ecto

try:
    import ecto_ros.ecto_ros_main
    import ecto_ros.ecto_sensor_msgs as ecto_sensor_msgs
    ECTO_ROS_FOUND = True
except ImportError:
    ECTO_ROS_FOUND = False

class TodDetector(ecto.BlackBox, DetectorBase):
    def __init__(self, *args, **kwargs):
        ecto.BlackBox.__init__(self, *args, **kwargs)
        DetectorBase.__init__(self)

    def declare_cells(self, p):
        guess_params = {}
        guess_params['visualize'] = p.visualize
        guess_params['db'] = p.db

        cells = {'depth_map': CellInfo(RescaledRegisteredDepth),
                 'feature_descriptor': CellInfo(FeatureDescriptor),
                 'guess_generator': CellInfo(ecto_detection.GuessGenerator, guess_params),
                 'passthrough': CellInfo(ecto.PassthroughN, {'items':{'image':'An image', 'K':'The camera matrix'}})}
        if ECTO_ROS_FOUND:
            cells['message_cvt'] = CellInfo(ecto_ros.ecto_ros_main.Mat2Image)

        return cells

    @classmethod
    def declare_forwards(cls, _p):
        p = {'feature_descriptor': [Forward('json_feature_params'),
                                    Forward('json_descriptor_params')],
             'guess_generator': [Forward('n_ransac_iterations'),
                                 Forward('min_inliers'),
                                 Forward('sensor_error')]}
        if ECTO_ROS_FOUND:
            p['message_cvt'] = [Forward('frame_id', 'rgb_frame_id')]
        i = {'passthrough': [Forward('image'), Forward('K')],
             'feature_descriptor': [Forward('mask')],
             'depth_map': [Forward('depth')]}

        o = {'feature_descriptor': [Forward('keypoints')],
             'guess_generator': [Forward('pose_results')]}

        return (p, i, o)

    @classmethod
    def declare_direct_params(self, p):
        p.declare('db', 'The DB to get data from as a JSON string', '{}')
        p.declare('search', 'The search parameters as a JSON string', '{}')
        p.declare('object_ids', 'The ids of the objects to find as a JSON list or the keyword "all".', 'all')
        p.declare('visualize', 'If true, some windows pop up to see the progress', False)

    def configure(self, p, _i, _o):
        self.descriptor_matcher = ecto_detection.DescriptorMatcher("Matcher",
                            search_json_params=p['search'],
                            json_object_ids=p['object_ids'])

        self._depth_map = RescaledRegisteredDepth()
        self._points3d = DepthTo3d()

    def connections(self, p):
        # Rescale the depth image and convert to 3d
        graph = [ self.passthrough['image'] >> self._depth_map['image'],
                  self._depth_map['depth'] >> self._points3d['depth'],
                  self.passthrough['K'] >> self._points3d['K'],
                  self._points3d['points3d'] >> self.guess_generator['points3d'] ]
        # make sure the inputs reach the right cells
        if 'depth' in self.feature_descriptor.inputs.keys():
            graph += [ self._depth_map['depth'] >> self.feature_descriptor['depth']]

        graph += [ self.passthrough['image'] >> self.feature_descriptor['image'],
                   self.passthrough['image'] >> self.guess_generator['image'] ]

        graph += [ self.descriptor_matcher['spans'] >> self.guess_generator['spans'],
                   self.descriptor_matcher['object_ids'] >> self.guess_generator['object_ids'] ]

        graph += [ self.feature_descriptor['keypoints'] >> self.guess_generator['keypoints'],
                   self.feature_descriptor['descriptors'] >> self.descriptor_matcher['descriptors'],
                   self.descriptor_matcher['matches', 'matches_3d'] >> self.guess_generator['matches', 'matches_3d'] ]

        cvt_color = imgproc.cvtColor(flag=imgproc.RGB2GRAY)

        if p.visualize or ECTO_ROS_FOUND:
            draw_keypoints = features2d.DrawKeypoints()
            graph += [ self.passthrough['image'] >> cvt_color[:],
                           cvt_color[:] >> draw_keypoints['image'],
                           self.feature_descriptor['keypoints'] >> draw_keypoints['keypoints']
                           ]

        if p.visualize:
            # visualize the found keypoints
            image_view = highgui.imshow(name="RGB")
            keypoints_view = highgui.imshow(name="Keypoints")

            graph += [ self.passthrough['image'] >> image_view['image'],
                       draw_keypoints['image'] >> keypoints_view['image']
                           ]

            pose_view = highgui.imshow(name="Pose")
            pose_drawer = calib.PosesDrawer()

            # draw the poses
            graph += [ self.passthrough['image', 'K'] >> pose_drawer['image', 'K'],
                       self.guess_generator['Rs', 'Ts'] >> pose_drawer['Rs', 'Ts'],
                       pose_drawer['output'] >> pose_view['image'] ]

        if ECTO_ROS_FOUND:
            ImagePub = ecto_sensor_msgs.Publisher_Image
            pub_features = ImagePub("Features Pub", topic_name='features')
            graph += [ draw_keypoints['image'] >> self.message_cvt[:],
                       self.message_cvt[:] >> pub_features[:] ]

        return graph
