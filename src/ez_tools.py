#!/usr/bin/env python
import time
import rospy
import random
import moveit_commander

from grasp_planning_graspit_msgs.srv import AddToDatabaseRequest, LoadDatabaseModelRequest
from ez_pick_and_place.srv import EzSceneSetupResponse, EzStartPlanning
from household_objects_database_msgs.msg import DatabaseModelPose
from geometry_msgs.msg import TransformStamped, PoseStamped, Pose
from manipulation_msgs.msg import GraspableObject
from manipulation_msgs.srv import GraspPlanning
from moveit_msgs.srv import GetPositionIKRequest
from std_srvs.srv import Trigger

# TODO check eef points with the service below
# and if it is successful, migrate everything to C++
# to avoid service calls
from moveit_msgs.srv import GetPositionIK

class EZToolSet():

    object_to_grasp = ""
    arm_move_group = None
    robot_commander = None
    arm_move_group_name = ""
    gripper_move_group_name = ""

    tf2_buffer = None
    tf2_listener = None

    moveit_scene = None
    planning_srv = None
    add_model_srv = None
    load_model_srv = None

    keep_planning = True

    ez_objects = dict()
    ez_obstacles = dict()

    gripper_name = None
    gripper_frame = None

    place_poses = []
    grasp_poses = []
    neargrasp_poses = []
    nearplace_poses = []

    compute_ik_srv = None

    def stopPlanning(self, req):
        self.keep_planning = False
        return True, ""

    def move(self, pose):
        self.arm_move_group.set_pose_target(pose)
        return self.arm_move_group.go()

    def moveMultiple(self, poses):
        self.arm_move_group.set_pose_targets(poses)
        return self.arm_move_group.go()

    def lookup_tf(self, target_frame, source_frame):
        return self.tf2_buffer.lookup_transform(target_frame, source_frame, rospy.Time(), rospy.Duration(10))

    def graspThis(self, object_name):
        dbmp = DatabaseModelPose()
        dbmp.model_id = self.ez_objects[object_name][0]
        dbmp.confidence = 1
        dbmp.detector_name = "manual_detection"
        planning_req = GraspPlanning()
        target = GraspableObject()
        target.reference_frame_id = "1"
        target.potential_models = [dbmp]
        response = self.planning_srv(arm_name = self.gripper_name, target = target)

        return response.grasps

    def attachThis(self, object_name):
        touch_links = self.robot_commander.get_link_names(self.gripper_move_group_name)
        self.arm_move_group.attach_object(object_name, link_name=self.arm_move_group.get_end_effector_link(), touch_links=touch_links)
        # self.moveit_scene.attach_mesh(self.arm_move_group.get_end_effector_link(), name=object_name, pose=None, touch_links=touch_links)

    def detachThis(self, object_name):
        self.arm_move_group.detach_object(object_name)

    def uberPlan(self, target, target_object):
        if self.pick():
            time.sleep(2)
            t = self.calcTargetPoseBasedOnCurrentState(target, target_object)
            self.attachThis(self.object_to_grasp)
            if self.move(t):
                self.detachThis(self.object_to_grasp)
                return True
        return False

    def pick(self):
        print "Starting now!"

        valid_preg = self.discard(self.neargrasp_poses)
        valid_g = self.discard(self.grasp_poses)

        print "valid_preg"
        print len(valid_preg)
        print "valid_g"
        print len(valid_g)

        for i in xrange(len(valid_preg[0])):
            for j in xrange(len(valid_g[0])):
                self.arm_move_group.set_start_state_to_current_state()
                plan1 = self.arm_move_group.plan()
                if self.moveMultiple([valid_preg[0][i].pose, valid_g[0][j].pose]):
                    return True
        return False

    def place(self):
        valid_postg = self.discard(self.neargrasp_poses)
        valid_prep = self.discard(self.nearplace_poses)
        valid_p = self.discard(self.place_poses)
        #valid_postp = self.discard(self.nearplace_poses)
        print "valid_postg"
        print len(valid_postg)
        print "valid_prep"
        print len(valid_prep)
        print "valid_p"
        print len(valid_p)
        #print "valid_postp"
        #print len(valid_postp)
        for k in xrange(len(valid_postg[0])):
            for l in xrange(len(valid_prep[0])):
                for m in xrange(len(valid_p[0])):
                    if self.moveMultiple([valid_postg[0][k].pose, valid_prep[0][l].pose, valid_p[0][m].pose]):
                        self.detachThis(self.object_to_grasp)
                        print valid_p[0][m].pose
                        return True
        return False

    def discard(self, poses):
        validp = []
        validrs = []
        req = GetPositionIKRequest()
        req.ik_request.group_name = self.arm_move_group_name
        req.ik_request.robot_state = self.robot_commander.get_current_state()
        req.ik_request.avoid_collisions = True
        for p in poses:
            req.ik_request.pose_stamped = p
            k = self.compute_ik_srv(req)
            if k.error_code.val == 1:
                validp.append(p)
                validrs.append(k.solution)
        return [validp, validrs]

    def startPlanningCallback(self, req):
        # TODO enable replanning
        # Initialize moveit stuff
        self.robot_commander = moveit_commander.RobotCommander()
        self.arm_move_group = moveit_commander.MoveGroupCommander(req.arm_move_group)
        self.arm_move_group_name = req.arm_move_group
        self.object_to_grasp = req.graspit_target_object
        self.gripper_move_group_name = req.gripper_move_group
        # Call graspit
        graspit_grasps = self.graspThis(req.graspit_target_object)
        # Generate grasp poses
        self.translateGraspIt2MoveIt(graspit_grasps, req.graspit_target_object)
        # Generate near grasp poses
        self.neargrasp_poses = self.generateNearPoses(self.grasp_poses)
        # Generate place poses
        self.place_poses = self.calcTargetPoses(self.grasp_poses, req.target_place)
        # Generate near place poses
        self.nearplace_poses = self.generateNearPoses(self.place_poses)

        return self.uberPlan(req.target_place, req.graspit_target_object), ""

    # Check if the input of the scene setup service is valid
    def validSceneSetupInput(self, req):
        tmp = dict()
        tmp2 = EzSceneSetupResponse()
        info = []
        error_codes = []
        if len(req.finger_joint_names) == 0:
            info.append("Invalid service input: No finger_joint_names provided")
            error_codes.append(tmp2.NO_FINGER_JOINTS)
            return False, info, error_codes
        if req.gripper.name == "":
            info.append("Invalid service input: No gripper name provided")
            error_codes.append(tmp2.NO_NAME)
            return False, info, error_codes
        if req.gripper.graspit_file == "":
            info.append("Invalid service input: No graspit filename provided for the gripper")
            error_codes.append(tmp2.NO_FILENAME)
            return False, info, error_codes
        if req.pose_factor <= 0:
            info.append("Invalid service input: pose_factor cannot be negative or zero")
            error_codes.append(tmp2.INVALID_POSE_FACTOR)
            return False, info, error_codes

        for obj in req.objects:
            if obj.name == "":
                info.append("Invalid service input: No object name provided")
                error_codes.append(tmp2.NO_NAME)
                return False, info, error_codes
            if obj.name in tmp:
                info.append("Invalid service input: Duplicate name: " + obj.name)
                error_codes.append(tmp2.DUPLICATE_NAME)
                return False, info, error_codes
            else:
                tmp[obj.name] = 0
            if obj.graspit_file == "" and obj.moveit_file == "":
                info.append("Invalid service input: No file provided for object: " + obj.name)
                error_codes.append(tmp2.NO_FILENAME)
                return False, info, error_codes
            if obj.pose.header.frame_id == "":
                info.append("Invalid service input: No frame_id in PoseStamped message of object: " + obj.name)
                error_codes.append(tmp2.NO_FRAME_ID)
                return False, info, error_codes

        for obs in req.obstacles:
            if obs.name == "":
                info.append("Invalid service input: No obstacle name provided")
                error_codes.append(tmp2.NO_NAME)
                return False, info, error_codes
            if obs.name in tmp:
                info.append("Invalid service input: Duplicate name: " + obs.name)
                error_codes.append(tmp2.DUPLICATE_NAME)
                return False, info, error_codes
            else:
                tmp[obs.name] = 0
            if obs.graspit_file == "" and obs.moveit_file == "":
                info.append("Invalid service input: No file provided for obstacle: " + obs.name)
                error_codes.append(tmp2.NO_FILENAME)
                return False, info, error_codes
            if obs.pose.header.frame_id == "":
                info.append("Invalid service input: No frame_id in PoseStamped message of obstacle: " + obs.name)
                error_codes.append(tmp2.NO_FRAME_ID)
                return False, info, error_codes
        return True, info, error_codes

    # Graspit bodies are always referenced relatively to the "world" frame
    def fixItForGraspIt(self, obj, pose_factor):
        p = Pose()
        if obj.pose.header.frame_id == "world":
            p.position.x = obj.pose.pose.position.x * pose_factor
            p.position.y = obj.pose.pose.position.y * pose_factor
            p.position.z = obj.pose.pose.position.z * pose_factor
            p.orientation.x = obj.pose.pose.orientation.x
            p.orientation.y = obj.pose.pose.orientation.y
            p.orientation.z = obj.pose.pose.orientation.z
            p.orientation.w = obj.pose.pose.orientation.w
            #TODO orientation?
        else:
            try:
                transform = TransformStamped()
                transform.header.stamp = rospy.Time.now()
                transform.header.frame_id = obj.pose.header.frame_id
                transform.child_frame_id = "ez_fix_it_for_grasp_it"
                transform.transform.translation.x = obj.pose.pose.position.x
                transform.transform.translation.y = obj.pose.pose.position.y
                transform.transform.translation.z = obj.pose.pose.position.z
                transform.transform.rotation.x = obj.pose.pose.orientation.x
                transform.transform.rotation.y = obj.pose.pose.orientation.y
                transform.transform.rotation.z = obj.pose.pose.orientation.z
                transform.transform.rotation.w = obj.pose.pose.orientation.w
                self.tf2_buffer.set_transform(transform, "fixItForGraspIt")

                trans = self.lookup_tf("ez_fix_it_for_grasp_it", "world")

                p.position.x = trans.transform.translation.x * pose_factor
                p.position.y = trans.transform.translation.y * pose_factor
                p.position.z = trans.transform.translation.z * pose_factor
                p.orientation.x = trans.transform.rotation.x
                p.orientation.y = trans.transform.rotation.y
                p.orientation.z = trans.transform.rotation.z
                p.orientation.w = trans.transform.rotation.w
            except Exception as e:
                print e

        return p

    # GraspIt and MoveIt appear to have a 90 degree difference in the x axis (roll 90 degrees)
    def translateGraspIt2MoveIt(self, grasps, object_name):
        self.grasp_poses = []
        for g in grasps:
            try:
                # World -> Object
                transform = TransformStamped()
                transform.header.stamp = rospy.Time.now()
                transform.header.frame_id = "world"
                transform.child_frame_id = "target_object_frame"
                transform.transform.translation.x = self.ez_objects[object_name][1].pose.position.x
                transform.transform.translation.y = self.ez_objects[object_name][1].pose.position.y
                transform.transform.translation.z = self.ez_objects[object_name][1].pose.position.z
                transform.transform.rotation.x = self.ez_objects[object_name][1].pose.orientation.x
                transform.transform.rotation.y = self.ez_objects[object_name][1].pose.orientation.y
                transform.transform.rotation.z = self.ez_objects[object_name][1].pose.orientation.z
                transform.transform.rotation.w = self.ez_objects[object_name][1].pose.orientation.w
                self.tf2_buffer.set_transform(transform, "ez_helper")

                # Object -> Gripper
                transform = TransformStamped()
                transform.header.stamp = rospy.Time.now()
                transform.header.frame_id = "target_object_frame"
                transform.child_frame_id = "ez_helper_graspit_pose"
                transform.transform.translation.x = g.grasp_pose.pose.position.x
                transform.transform.translation.y = g.grasp_pose.pose.position.y
                transform.transform.translation.z = g.grasp_pose.pose.position.z
                transform.transform.rotation.x = g.grasp_pose.pose.orientation.x
                transform.transform.rotation.y = g.grasp_pose.pose.orientation.y
                transform.transform.rotation.z = g.grasp_pose.pose.orientation.z
                transform.transform.rotation.w = g.grasp_pose.pose.orientation.w
                self.tf2_buffer.set_transform(transform, "ez_helper")

                transform_frame_gripper_trans = self.lookup_tf(self.arm_move_group.get_end_effector_link(), self.gripper_frame)

                # Gripper -> End Effector
                transform = TransformStamped()
                transform.header.stamp = rospy.Time.now()
                transform.header.frame_id = "ez_helper_graspit_pose"
                transform.child_frame_id = "ez_helper_fixed_graspit_pose"
                transform.transform.translation.x = -transform_frame_gripper_trans.transform.translation.x
                transform.transform.translation.y = -transform_frame_gripper_trans.transform.translation.y
                transform.transform.translation.z = -transform_frame_gripper_trans.transform.translation.z
                transform.transform.rotation.x = transform_frame_gripper_trans.transform.rotation.x
                transform.transform.rotation.y = transform_frame_gripper_trans.transform.rotation.y
                transform.transform.rotation.z = transform_frame_gripper_trans.transform.rotation.z
                transform.transform.rotation.w = transform_frame_gripper_trans.transform.rotation.w
                self.tf2_buffer.set_transform(transform, "ez_helper")

                # Graspit to MoveIt translation
                # (Gripper -> Gripper)
                graspit_moveit_transform = TransformStamped()
                graspit_moveit_transform.header.stamp = rospy.Time.now()
                graspit_moveit_transform.header.frame_id = "ez_helper_fixed_graspit_pose"
                graspit_moveit_transform.child_frame_id = "ez_helper_target_graspit_pose"
                graspit_moveit_transform.transform.rotation.x = 0.7071
                graspit_moveit_transform.transform.rotation.y = 0.0
                graspit_moveit_transform.transform.rotation.z = 0.0
                graspit_moveit_transform.transform.rotation.w = 0.7071
                self.tf2_buffer.set_transform(graspit_moveit_transform, "ez_helper")

                target_trans = self.lookup_tf("world", "ez_helper_target_graspit_pose")

                g.grasp_pose.header.frame_id = "world"
                g.grasp_pose.pose.position.x = target_trans.transform.translation.x
                g.grasp_pose.pose.position.y = target_trans.transform.translation.y
                g.grasp_pose.pose.position.z = target_trans.transform.translation.z
                g.grasp_pose.pose.orientation.x = target_trans.transform.rotation.x
                g.grasp_pose.pose.orientation.y = target_trans.transform.rotation.y
                g.grasp_pose.pose.orientation.z = target_trans.transform.rotation.z
                g.grasp_pose.pose.orientation.w = target_trans.transform.rotation.w
                self.grasp_poses.append(g.grasp_pose)
            except Exception as e:
                print e

    def calcNearPose(self, pose):
        # TODO fix the near strategy
        near_pose = PoseStamped()
        near_pose.header = pose.header
        near_pose.pose.position.x = pose.pose.position.x + random.uniform(-0.05, 0.05)
        near_pose.pose.position.y = pose.pose.position.y + random.uniform(-0.05, 0.05)
        near_pose.pose.position.z = pose.pose.position.z + random.uniform(-0.05, 0.15)
        near_pose.pose.orientation = pose.pose.orientation
        return near_pose

    def calcTargetPose(self, pose, grasp_pose):
        # TODO fix the situation of an exception
        # Currently, we are doomed
        target_pose = PoseStamped()
        if pose.header.frame_id != "world":
            try:
                transform = TransformStamped()
                transform.header.stamp = rospy.Time.now()
                transform.header.frame_id = pose.header.frame_id
                transform.child_frame_id = "ez_target_pose_calculator"
                transform.transform.translation.x = pose.pose.position.x
                transform.transform.translation.y = pose.pose.position.y
                transform.transform.translation.z = pose.pose.position.z
                transform.transform.rotation.x = pose.pose.orientation.x
                transform.transform.rotation.y = pose.pose.orientation.y
                transform.transform.rotation.z = pose.pose.orientation.z
                transform.transform.rotation.w = pose.pose.orientation.w
                self.tf2_buffer.set_transform(transform, "calcTargetPose")

                trans = self.lookup_tf("world", "ez_target_pose_calculator")

                target_pose.header.stamp = rospy.Time.now()
                target_pose.header.frame_id = "world"
                target_pose.pose.position.x = trans.transform.translation.x
                target_pose.pose.position.y = trans.transform.translation.y
                target_pose.pose.position.z = trans.transform.translation.z
            except Exception as e:
                print e
        else:
            target_pose.header = pose.header
            target_pose.pose.position.x = pose.pose.position.x
            target_pose.pose.position.y = pose.pose.position.y
            target_pose.pose.position.z = pose.pose.position.z

        target_pose.pose.orientation = grasp_pose.pose.orientation
        target_pose.pose.position.z = grasp_pose.pose.position.z + 0.01
        return target_pose

    def calcTargetPoses(self, grasp_poses, pose):
        # TODO fix the situation of an exception
        # Currently, we are doomed
        target_poses = []
        target_pose = PoseStamped()
        if pose.header.frame_id != "world":
            try:
                transform = TransformStamped()
                transform.header.stamp = rospy.Time.now()
                transform.header.frame_id = pose.header.frame_id
                transform.child_frame_id = "ez_target_pose_calculator"
                transform.transform.translation.x = pose.pose.position.x
                transform.transform.translation.y = pose.pose.position.y
                transform.transform.translation.z = pose.pose.position.z
                transform.transform.rotation.x = pose.pose.orientation.x
                transform.transform.rotation.y = pose.pose.orientation.y
                transform.transform.rotation.z = pose.pose.orientation.z
                transform.transform.rotation.w = pose.pose.orientation.w
                self.tf2_buffer.set_transform(transform, "calcTargetPose")

                trans = self.lookup_tf("world", "ez_target_pose_calculator")

                target_pose.header.stamp = rospy.Time.now()
                target_pose.header.frame_id = "world"
                target_pose.pose.position.x = trans.transform.translation.x
                target_pose.pose.position.y = trans.transform.translation.y
                target_pose.pose.position.z = trans.transform.translation.z
            except Exception as e:
                print e
        else:
            target_pose.header = pose.header
            target_pose.pose.position.x = pose.pose.position.x
            target_pose.pose.position.y = pose.pose.position.y
            target_pose.pose.position.z = pose.pose.position.z


        for grasp_pose in grasp_poses:
            target_pose_ = PoseStamped()
            target_pose_.header = target_pose.header
            target_pose_.pose.position.x = target_pose.pose.position.x
            target_pose_.pose.position.y = target_pose.pose.position.y
            target_pose_.pose.position.z = grasp_pose.pose.position.z + 0.01
            target_pose_.pose.orientation = grasp_pose.pose.orientation
            target_poses.append(target_pose_)
        return target_poses

    def calcTargetPoseBasedOnCurrentState(self, target, target_object):
        # TODO Handle exception
        try:
            t = self.moveit_scene.get_object_poses([target_object])

            # TODO This lookup is different than tf_echo... (HOW?!)
            trans = self.lookup_tf("world", self.arm_move_group.get_end_effector_link())
            print "trans"
            print trans

            obj = TransformStamped()
            obj.header.stamp = rospy.Time.now()
            obj.header.frame_id = "world"
            obj.child_frame_id = "ez_target_pick"
            obj.transform.translation.x = t[target_object].position.x
            obj.transform.translation.y = t[target_object].position.y
            obj.transform.translation.z = t[target_object].position.z
            obj.transform.rotation.x = t[target_object].orientation.x
            obj.transform.rotation.y = t[target_object].orientation.y
            obj.transform.rotation.z = t[target_object].orientation.z
            obj.transform.rotation.w = t[target_object].orientation.w
            self.tf2_buffer.set_transform(obj, "calcTargetPose")

            trans1 = self.lookup_tf("ez_target_pick", self.arm_move_group.get_end_effector_link())
            print "trans1"
            print trans1

            target_trans = TransformStamped()
            target_trans.header.stamp = rospy.Time.now()
            target_trans.header.frame_id = target.header.frame_id
            target_trans.child_frame_id = "ez_target_place"
            target_trans.transform.translation.x = target.pose.position.x
            target_trans.transform.translation.y = target.pose.position.y
            target_trans.transform.translation.z = target.pose.position.z
            target_trans.transform.rotation.x = target.pose.orientation.x
            target_trans.transform.rotation.y = target.pose.orientation.y
            target_trans.transform.rotation.z = target.pose.orientation.z
            target_trans.transform.rotation.w = target.pose.orientation.w
            self.tf2_buffer.set_transform(target_trans, "calcTargetPose")

            ee_target_trans = TransformStamped()
            ee_target_trans.header.stamp = rospy.Time.now()
            ee_target_trans.header.frame_id = "ez_target_place"
            ee_target_trans.child_frame_id = "ez_target_to_ee"
            ee_target_trans.transform.translation.x = trans1.transform.translation.x
            ee_target_trans.transform.translation.y = trans1.transform.translation.y
            ee_target_trans.transform.translation.z = trans1.transform.translation.z
            ee_target_trans.transform.rotation.x = trans1.transform.rotation.x
            ee_target_trans.transform.rotation.y = trans1.transform.rotation.y
            ee_target_trans.transform.rotation.z = trans1.transform.rotation.z
            ee_target_trans.transform.rotation.w = trans1.transform.rotation.w
            self.tf2_buffer.set_transform(ee_target_trans, "calcTargetPose")

            trans2 = self.lookup_tf("world", "ez_target_to_ee")
            print "trans2"
            print trans2

            target_pose = PoseStamped()
            target_pose.header.stamp = rospy.Time.now()
            target_pose.header.frame_id = "world"
            target_pose.pose.position.x = trans2.transform.translation.x
            target_pose.pose.position.y = trans2.transform.translation.y
            target_pose.pose.position.z = trans.transform.translation.z + 0.01
            target_pose.pose.orientation.x = trans.transform.rotation.x
            target_pose.pose.orientation.y = trans.transform.rotation.y
            target_pose.pose.orientation.z = trans.transform.rotation.z
            target_pose.pose.orientation.w = trans.transform.rotation.w
            print target_pose
            return target_pose
        except Exception as e:
            print e


    def scene_setup(self, req):
        valid, info, ec = self.validSceneSetupInput(req)

        self.gripper_frame = req.gripper_frame

        if not valid:
            return valid, info, ec

        res = EzSceneSetupResponse()
        res.success = True

        try:
            for obj in req.objects:
                # ------ Graspit world ------
                if obj.graspit_file != "":
                    atd = AddToDatabaseRequest()
                    atd.filename = obj.graspit_file
                    atd.isRobot = False
                    atd.asGraspable = True
                    atd.modelName = obj.name
                    response = self.add_model_srv(atd)
                    if response.returnCode != response.SUCCESS:
                        res.success = False
                        res.info.append("Error adding object " + obj.name + " to graspit database")
                        res.error_codes.append(response.returnCode)
                    else:
                        objectID = response.modelID

                        loadm = LoadDatabaseModelRequest()
                        loadm.model_id = objectID
                        loadm.model_pose = self.fixItForGraspIt(obj, req.pose_factor)
                        response = self.load_model_srv(loadm)

                        self.ez_objects[obj.name] = [objectID, obj.pose]

                        if response.result != response.LOAD_SUCCESS:
                            res.success = False
                            res.info.append("Error loading object " + obj.name + " to graspit world")
                            res.error_codes.append(response.result)
                # ---------------------------

                # ------ Moveit scene -------
                if obj.moveit_file != "":
                    self.moveit_scene.add_mesh(obj.name, obj.pose, obj.moveit_file)
                # ---------------------------
            for obstacle in req.obstacles:
                # ------ Graspit world ------
                if obstacle.graspit_file != "":
                    atd = AddToDatabaseRequest()
                    atd.filename = obstacle.graspit_file
                    atd.isRobot = False
                    atd.asGraspable = False
                    atd.modelName = obstacle.name
                    response = self.add_model_srv(atd)
                    if response.returnCode != response.SUCCESS:
                        res.success = False
                        res.info.append("Error adding obstacle " + obstacle.name + " to graspit database")
                        res.error_codes.append(response.returnCode)
                    else:
                        obstacleID = response.modelID

                        loadm = LoadDatabaseModelRequest()
                        loadm.model_id = obstacleID
                        loadm.model_pose = self.fixItForGraspIt(obstacle, req.pose_factor)
                        response = self.load_model_srv(loadm)

                        self.ez_obstacles[obstacle.name] = [obstacleID, obstacle.pose]

                        if response.result != response.LOAD_SUCCESS:
                            res.success = False
                            res.info.append("Error loading obstacle " + obstacle.name + " to graspit world")
                            res.error_codes.append(response.result)
                # ---------------------------

                # ------ Moveit scene -------
                if obstacle.moveit_file != "":
                    self.moveit_scene.add_mesh(obstacle.name, obstacle.pose, obstacle.moveit_file)
                # ---------------------------

            # ------ Graspit world ------
            atd = AddToDatabaseRequest()
            atd.filename = req.gripper.graspit_file
            atd.isRobot = True
            atd.asGraspable = False
            atd.modelName = req.gripper.name
            atd.jointNames = req.finger_joint_names
            response = self.add_model_srv(atd)
            if response.returnCode != response.SUCCESS:
                    res.success = False
                    res.info.append("Error adding robot " + req.gripper.name + " to graspit database")
                    res.error_codes.append(response.returnCode)
            else:
                self.gripper_name = req.gripper.name
                robotID = response.modelID

                loadm = LoadDatabaseModelRequest()
                loadm.model_id = robotID
                p = Pose()

                gripper_trans = self.lookup_tf(self.gripper_frame, "world")

                p.position.x = gripper_trans.transform.translation.x * req.pose_factor
                p.position.y = gripper_trans.transform.translation.y * req.pose_factor
                p.position.z = gripper_trans.transform.translation.z * req.pose_factor
                # TODO orientation is not important (right?)
                loadm.model_pose = p
                response = self.load_model_srv(loadm)

                if response.result != response.LOAD_SUCCESS:
                    res.success = False
                    res.info.append("Error loading robot " + req.gripper.name + " to graspit world")
                    res.error_codes.append(response.result)
            # ---------------------------

            return res

        except Exception as e:
            info.append(str(e))
            ec.append(res.EXCEPTION)
            return False, info, ec

    def generateNearPoses(self, grasps):
        poses = []
        for g in grasps:
            p = self.calcNearPose(g)
            while p in poses:
                p = self.calcNearPose(g)
            poses.append(p)
        return poses

