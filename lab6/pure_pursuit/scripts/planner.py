#!/usr/bin/env python3
from __future__ import print_function
import sys
import math
from traceback import print_tb
import numpy as np
import os
import tf

#ROS Imports
import rospy
import rosbag
import geometry_msgs
from nav_msgs.msg import OccupancyGrid
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path

# scikit-image for image operations
from skimage import io, morphology, img_as_ubyte, __version__

MAX_FLOAT = 99999.9  # avoid problems with python2
DEBUG = False

class planner:
    def __init__(self):
        rospy.loginfo("Hello from planner node!")
        rospy.loginfo("scikit-image version " + __version__)

        # Parameters
        self.IS_SLAM_MAP = rospy.get_param('/planner/is_slam_map', "False")  # image created with cartographer?
        self.MAP_NAME = rospy.get_param('/planner/map_name', "")  # name for the exported map
        self.BAG_NAME = rospy.get_param('/planner/bag_name', "")  # name for the exported rosbag
        
        # Path for saving maps
        self.savepath = os.path.dirname(os.path.realpath(__file__)) + "/../maps"
        if not os.path.exists(self.savepath):
            os.makedirs(self.savepath)

        self.OCCUPIED_THRESHOLD = 0.65  # map pixels are considered occupied if larger than this value
        self.SAFETY_DISTANCE = rospy.get_param('/planner/wall_distance', 0.6)  # safety foam radius (m)
        self.WAYPOINT_DISTANCE = rospy.get_param('/planner/waypoint_distance', 0.1)  # distance between waypoints (m)
        self.START_LINE_STEP = rospy.get_param('/planner/start_line_step', 4)  # in pixels   

        self.STEP_SIZE = rospy.get_param('/planner/gradient_step_size', 5 * np.sqrt(2)) 
        self.WEIGHT_PAST = rospy.get_param('/planner/gradient_weight_past', 0.35) 
        self.WEIGHT_FUTURE = rospy.get_param('/planner/gradient_weight_future', 0.15)
        self.CLEARANCE_WEIGHT = rospy.get_param('/planner/clearance_weight', 0.33) 
        self.MAX_REASONABLE_ANGLE = math.radians(60.0)
        self.MIN_LAP_LENGTH = 20.0

        # False: basic approach as in assignment sheet
        # True:  gradient descent based path calculation with larger step size
        self.USE_GRADIENT_DESCENT = rospy.get_param('/planner/use_gradient_descent', True)
        
         # Topics & Subscriptions, Publishers
        self.map_topic = '/map'
        self.path_topic = '/path'

        self.map_sub = rospy.Subscriber(self.map_topic, OccupancyGrid, self.map_callback, queue_size=1)
        self.path_pub = rospy.Publisher(self.path_topic, Path, queue_size=10, latch=True)

    def map_callback(self, data):
        """
        Process the map to pre-compute the path to follow and publish it
        """
        rospy.loginfo("map info from OccupancyGrid message:\n%s", data.info)
        
        self.resolution = data.info.resolution
        if self.IS_SLAM_MAP:
            self.shape = (data.info.height, data.info.width)
            self.start_position = (data.info.origin.position.y,
                                data.info.origin.position.x)
        else:
            self.shape = (data.info.width, data.info.height)
            self.start_position = (data.info.origin.position.x,
                                data.info.origin.position.y)

        self.start_pixel = (int(-self.start_position[0]/self.resolution),
                            int(-self.start_position[1]/self.resolution))

        print(self.start_pixel)

        # in pixels
        self.WAYPOINT_DISTANCE = rospy.get_param('/planner/waypoint_distance', 0.8)/self.resolution
        
        map = self.preprocess_map(data)

        driveable_area = self.get_driveable_area(map)
        if DEBUG:
            self.save_map(driveable_area, "1_drivable_area")

        driveable_area = self.add_safety_foam(driveable_area)
        if DEBUG:
            self.save_map(driveable_area, "2_drivable_area_safety")

        clearances = self.get_clearances(driveable_area)
        if DEBUG:
            self.save_map(clearances/np.max(clearances), "3_clearances")

        shortest_lap = MAX_FLOAT

        # plan path for different starting positions, consider the shortest round-trip
        for start_x in range(self.start_line_left+1, self.start_line_right, self.START_LINE_STEP):

            path_msg = Path()
            path_msg.header.frame_id = "map"
            path_msg.header.stamp = rospy.get_rostime() 

            distances = self.get_distances(driveable_area, start_x)
        
            path, path_msg, lap_length = self.calculate_path(map, distances, clearances, path_msg, start_x=start_x)

            # lap_length = distances[start_x, self.start_pixel[1] + 2]
            rospy.loginfo("Start @" + str(start_x) + ": track length " + str(lap_length))
            if lap_length <= shortest_lap and lap_length > self.MIN_LAP_LENGTH:
                shortest_lap = lap_length
                self.start_x = start_x
                self.distances = distances
                self.path = path
                self.path_msg = path_msg

        self.lap_length = shortest_lap
        rospy.loginfo("Shortest Lap: Start @" + str(self.start_x) + ": track length " + str(self.lap_length))

        distances_print = self.distances.copy()
        distances_print[driveable_area == False] = 0.0  # remove high values outside driveable area
        if DEBUG:
            self.save_map(distances_print/np.max(distances_print), "4_distances")

        self.path_pub.publish(self.path_msg)
        self.save_map(self.path, "path")

        try:
            bag = rosbag.Bag(self.BAG_NAME + '.bag', 'w')
            bag.write(self.path_topic, self.path_msg)
            rospy.loginfo("Bag file written to " + self.BAG_NAME + '.bag')
        finally:
            bag.close()

    def get_distances(self, driveable_area, start_x):
        distances = np.full(self.shape, MAX_FLOAT, dtype=float)
        done_map = (~(driveable_area)).copy()

        # Mark starting line as done, distance 0 for start position
        y = self.start_pixel[1]
        for x in range(self.start_line_left, self.start_line_right+1):
            done_map[x, y] = True
            distances[x, y] =  abs(x-start_x)

        queue = []
        queue.append((self.start_pixel[0], self.start_pixel[1]-1))

        queued_map = done_map.copy()
        while queue:
            (x, y) = queue.pop(0)
            min_dist, min_x, min_y = self.get_min_dist_neighbour(done_map, distances, x, y)
            done_map[x, y] = True
            new_dist = np.linalg.norm(np.array([x, y]) - np.array([min_x, min_y])) + min_dist
            if new_dist < distances[x, y]:
                distances[x, y] = new_dist
            for [step_x, step_y] in [[0, 1],[0, -1],[1, 0],[-1, 0],[1, 1],[-1, 1],[1, -1],[-1, -1]]:
                new_x = x+step_x
                new_y = y+step_y
                if not done_map[new_x, new_y] and not queued_map[new_x, new_y]:
                    done_map[new_x, new_y] = True
                    queue.append((new_x, new_y))
        return distances

    def get_clearances(self, driveable_area):
        OFFSET = 0.01  # prevents division by 0
        clearances = np.zeros(self.shape, dtype=float)

        indices = np.argwhere(driveable_area)

        # find track edge
        binary_image = morphology.binary_erosion(driveable_area)
        indices_out = np.argwhere(driveable_area!=binary_image) 

        for index in indices:
            clearances[index[0], index[1]] = np.min(np.linalg.norm(indices_out - index, axis=1))

        clearances = clearances**2 + OFFSET
        clearances = 1/clearances

        return clearances

    def preprocess_map(self, data):
        if self.IS_SLAM_MAP:
            map_data = np.asarray(data.data).reshape((data.info.height, data.info.width)) # parse map data into 2D numpy array
        else:
            map_data = np.asarray(data.data).reshape((data.info.width, data.info.height)) # parse map data into 2D numpy array

        map_normalized = map_data / np.amax(map_data.flatten()) # normalize map
        map_binary = map_normalized < 0.65  # make binary occupancy map
        return map_binary

    def get_driveable_area(self, map_binary):
        driveable_area = morphology.flood_fill(
            image = 1*map_binary,
            seed_point = self.start_pixel,
            new_value = -1,
        )
        driveable_area = driveable_area < 0
        return driveable_area

    def add_safety_foam(self, driveable_area):
        # selem for scikit-image 16.2, new name: footprint
        binary_image = morphology.binary_erosion(driveable_area, selem=morphology.disk(radius=self.SAFETY_DISTANCE/self.resolution, dtype=bool))

        x = self.start_pixel[0]+1
        while binary_image[x, self.start_pixel[1]]:
            x = x + 1
        self.start_line_right = x-1
        
        x = self.start_pixel[0]-1
        while binary_image[x, self.start_pixel[1]]:
            x = x - 1
        self.start_line_left = x+1
        return binary_image

    def get_min_dist_neighbour(self, done_map, distances, x, y):
        min_dist = MAX_FLOAT
        for [step_x, step_y] in [[0, 1],[0, -1],[1, 0],[-1, 0],[1, 1],[-1, 1],[1, -1],[-1, -1]]:
            if done_map[x+step_x, y+step_y]:
                if distances[x+step_x, y+step_y] < min_dist:
                    min_dist = distances[x+step_x, y+step_y]
                    x_min = x+step_x
                    y_min = y+step_y
        return min_dist, x_min, y_min

    def calculate_path(self, map, distances, clearances, path_msg, start_x):
        # Start a little above the starting line as the line has distance 0
        # The pixels just above have distance 1 due to an imperfect distance calculation
        START_OFFSET = 2

        x = start_x
        y = self.start_pixel[1] + START_OFFSET
        distance = distances[x, y]

        path_length = 1.0 * START_OFFSET
        path = map.copy()

        steering_effort = 0.0

        best_x = -1
        best_y = -1

        path[x,y] = 0.0
        # Fist step: only steps up (or sideways) as first gradient points towards line
        for [step_x, step_y] in [[0, 1],[1, 0],[-1, 0],[1, 1],[-1, 1]]:  
            new_x = x + step_x
            new_y = y + step_y
            if distances[new_x, new_y] < distance:
                distance = distances[new_x, new_y]
                best_x = new_x
                best_y = new_y

        # TODO steering effort

        if best_x == -1 and best_y == -1:
            rospy.logerr("No valid path found from this starting position. Make sure the SAFETY_DISTANCE does not block the track and consider increasing START_OFFSET.")
            exit(-1)
        
        last_x = x
        last_y = y
        x = best_x
        y = best_y
        last_waypoint = np.array([x, y])
        last_wp_orientation = np.arctan2(last_x-x, last_y-y) + np.pi

        # Not broadcasting a position at the start reduces problems with a non-central start
        # self.add_pose_to_path(path_msg, x, y, last_x=last_x, last_y=last_y)

        if not self.USE_GRADIENT_DESCENT:
            while(distance > 0.0):
                path[x,y] = 0.0
                path_point = np.array([x, y])
                
                if np.linalg.norm(path_point - last_waypoint) >= self.WAYPOINT_DISTANCE:
                    self.add_pose_to_path(path_msg, x, y, last_x=last_x, last_y=last_y)
                    path_length += np.linalg.norm(path_point - last_waypoint)
                    last_waypoint = path_point

                for [step_x, step_y] in [[0, 1], [0, -1],[1, 0],[-1, 0],[1, 1],[-1, 1],[1, -1],[-1, -1]]:
                    new_x = x+step_x
                    new_y = y+step_y
                    if distances[new_x, new_y] < distance:
                        distance = distances[new_x, new_y]
                        best_x = new_x
                        best_y = new_y
                last_x, last_y = x, y
                x, y = best_x, best_y
                
            # add final waypoint (to have a fair distance comparison)
            self.add_pose_to_path(path_msg, x, y, last_x=last_x, last_y=last_y)
            path_length += np.linalg.norm(path_point - last_waypoint)

        else:  # USE_GRADIENT_DESCENT
            field = (1-self.CLEARANCE_WEIGHT)*distances + self.CLEARANCE_WEIGHT * clearances
            g_x, g_y = np.gradient(field)
            orientation = np.arctan2(g_x, g_y)+np.pi

            # self.save_map((orientation+np.pi)/(np.max(orientation)* 2 * np.pi), "6_orientation")
            dir = orientation[x, y]

            i = 0
            while(distance > self.STEP_SIZE-1.0):
                path[x,y] = 0.0
                path_point = np.array([x, y])
                
                if np.linalg.norm(path_point - last_waypoint) >= self.WAYPOINT_DISTANCE:
                    wp_orientation = np.arctan2(last_waypoint[0]-path_point[0], last_waypoint[1]-path_point[1]) + np.pi
                    self.add_pose_to_path(path_msg, x, y, orientation=wp_orientation)
                    path_length += np.linalg.norm(path_point - last_waypoint)

                    ang = np.abs(wp_orientation-last_wp_orientation)
                    if ang > np.pi:
                        ang = ang - 2*np.pi
                    steering_effort += (ang/np.linalg.norm(path_point - last_waypoint))

                    last_waypoint = path_point
                    last_wp_orientation = wp_orientation
               

                if np.abs(dir - orientation[x, y]) < self.MAX_REASONABLE_ANGLE and \
                   np.abs(orientation[int(x + self.STEP_SIZE * np.sin(orientation[x,y])+0.5),
                          int(y + self.STEP_SIZE * np.cos(orientation[x,y])+0.5)] - orientation[x, y]
                         ) < self.MAX_REASONABLE_ANGLE:
                    dir = self.WEIGHT_PAST * dir + \
                          (1-self.WEIGHT_PAST - self.WEIGHT_FUTURE) * orientation[x, y] + \
                          self.WEIGHT_FUTURE * orientation[int(x + self.STEP_SIZE * np.sin(orientation[x,y])+0.5), int(y + self.STEP_SIZE * np.cos(orientation[x,y])+0.5)]
                    next_step = self.STEP_SIZE

                    new_x = int(x + next_step * np.sin(dir)+0.5)
                    new_y = int(y + next_step * np.cos(dir)+0.5)

                    j = 1
                    while distances[new_x, new_y] == MAX_FLOAT:
                        new_x = int(x + (next_step-j*np.sqrt(2)) * np.sin(dir)+0.5)
                        new_y = int(y + (next_step-j*np.sqrt(2)) * np.cos(dir)+0.5)
                        j = j + 1
                        if (next_step-j*np.sqrt(2)) <= 0 or (next_step-j*np.sqrt(2)) <= 0:
                            break
                else:
                    dir = orientation[x, y]
                    next_step = np.sqrt(2)

                last_x = x
                last_y = y
                x = int(x + next_step * np.sin(dir)+0.5)
                y = int(y + next_step * np.cos(dir)+0.5)
                distance = distances[x, y]

                i = i+1
                if i > 50000:
                    rospy.logerr("No path terminating at the start found. Consider reducing CLEARANCE_WEIGHT.")
                    exit(-1)

            # add final waypoint (to have a fair distance comparison)
            self.add_pose_to_path(path_msg, x, y, orientation=orientation[x, y])
            path_length += np.linalg.norm(path_point - last_waypoint)

            rospy.loginfo("Steering: " + str(steering_effort))
        return path, path_msg, path_length

    def add_pose_to_path(self, path_msg, x, y, last_x=-1, last_y=-1, orientation=0.0):
        pose_msg = PoseStamped()
        pose_msg.header.frame_id = "map"
        pose_msg.header.stamp = rospy.get_rostime() 
        if self.IS_SLAM_MAP:
            pose_msg.pose.position.x = self.resolution*y + self.start_position[1]
            pose_msg.pose.position.y = self.resolution*x + self.start_position[0]
        else:
            pose_msg.pose.position.x = self.resolution*y + self.start_position[0]
            pose_msg.pose.position.y = self.resolution*x + self.start_position[1]
        pose_msg.pose.position.z = 0.0

        if last_x == -1 and last_y == -1:
            orientation = orientation
        else:
            orientation = math.atan2(x-last_x, y-last_y)

        pose_msg.pose.orientation = geometry_msgs.msg.Quaternion(*tf.transformations.quaternion_from_euler(0.0, 0.0, orientation))
        path_msg.poses.append(pose_msg)

    def save_map(self, map, name=""):
        save_path = self.savepath + '/' +  self.MAP_NAME + "_" + name + '.png'
        map_image = np.rot90(np.flip(map, 0), 1) # flip and rotate for rendering as image in correct direction
        io.imsave(save_path, img_as_ubyte(map_image), check_contrast=False) # save image, just to show the content of the 2D array for debug purposes
        rospy.loginfo("map saved to %s", save_path)

def main(args):
    rospy.init_node("planner_node", anonymous=True)
    rfgs = planner()
    rospy.sleep(0.1)
    rospy.spin()

if __name__ == '__main__':
    main(sys.argv)
