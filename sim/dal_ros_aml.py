#!/usr/bin/env python
from __future__ import print_function
import rospy
import actionlib
from move_base_msgs.msg import *
from sim.utils import *
from random_box_map import *
from navi import *

from sensor_msgs.msg import LaserScan
from nav_msgs.msg import Odometry
from geometry_msgs.msg import PoseStamped, Twist
from visualization_msgs.msg import Marker, MarkerArray

from tf.transformations import *

import numpy as np
from scipy import ndimage, interpolate
from collections import OrderedDict
import pdb
import glob
import os
import multiprocessing 
import errno
import re
import time
import random
import cv2
from recordtype import recordtype

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from torch.optim.lr_scheduler import ReduceLROnPlateau, StepLR

import torchvision
from torchvision import transforms
from torchvision.models.densenet import densenet121, densenet169, densenet201, densenet161
# from logger import Logger

from copy import deepcopy

from networks import policy_A3C

from resnet_pm import resnet18, resnet34, resnet50, resnet101, resnet152
from torchvision.models.resnet import resnet18 as resnet18s
from torchvision.models.resnet import resnet34 as resnet34s
from torchvision.models.resnet import resnet50 as resnet50s
from torchvision.models.resnet import resnet101 as resnet101s
from torchvision.models.resnet import resnet152 as resnet152s

from networks import intrinsic_model

import math
import argparse
from datetime import datetime
from maze import generate_map
import matplotlib.pyplot as plt
import matplotlib.colors as cm
from matplotlib.patches import Wedge
import matplotlib.gridspec as gridspec

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def shift(grid, d, axis=None, fill = 0.5):
    grid = np.roll(grid, d, axis=axis)
    if axis == 0:
        if d > 0:
            grid[:d,:] = fill
        elif d < 0:
            grid[d:,:] = fill
    elif axis == 1:
        if d > 0:
            grid[:,:d] = fill
        elif d < 0:
            grid[:,d:] = fill
    return grid

def softmax(w, t = 1.0):
    e = np.exp(np.array(w) / t)
    dist = e / np.sum(e)
    return dist

def softermax(w, t = 1.0):
    w = np.array(w)
    w = w - w.min() + np.exp(1)
    e = np.log(w)
    dist = e / np.sum(e)
    return dist


def normalize(x):
    if x.min() == x.max():
        return 0.0*x
    x = x-x.min()
    x = x/x.max()
    return x


Pose2d = recordtype("Pose2d", "theta x y")
Grid = recordtype("Grid", "head row col")

class Lidar():
    def __init__(self, ranges, angle_min, angle_max,
                 range_min, range_max, noise=0):
        # self.ranges = np.clip(ranges, range_min, range_max)
        self.ranges = np.array(ranges)
        self.angle_min = angle_min
        self.angle_max = angle_max
        num_data = len(self.ranges)
        self.angle_increment = (self.angle_max-self.angle_min)/num_data #math.increment
        self.angles_2pi= np.linspace(angle_min, angle_max, len(ranges), endpoint=True) % (2*np.pi)
        idx = np.argsort(self.angles_2pi)
        self.ranges_2pi = self.ranges[idx]
        self.angles_2pi = self.angles_2pi[idx]
        


class LocalizationNode(object):
    def __init__(self, args):

        self.next_action = None
        self.skip_to_end = False
        self.action_time = 0
        self.gtl_time = 0
        self.lm_time = 0
        # self.wait_for_scan = False
        self.scan_once = False
        self.scan_bottom_once = False
        self.scan_on = False
        self.scan_ready = False
        self.scan_bottom_ready = False
        self.wait_for_move = False
        self.robot_pose_ready = False
        self.args = args
        self.rl_test = False
        self.start_time = time.time()

        if (self.args.use_gpu) > 0 and torch.cuda.is_available():
            self.device = torch.device("cuda" )
            torch.set_default_tensor_type(torch.cuda.FloatTensor)
        else:
            self.device = torch.device("cpu")
            torch.set_default_tensor_type(torch.FloatTensor)

        # self.args.n_maze_grids
        # self.args.n_local_grids
        # self.args.n_lm_grids

        self.init_fig = False
        self.n_maze_grids = None
        
        self.grid_rows = self.args.n_local_grids #self.args.map_size * self.args.sub_resolution
        self.grid_cols = self.args.n_local_grids #self.args.map_size * self.args.sub_resolution
        self.grid_dirs = self.args.n_headings

        num_dirs = 1

        num_classes = self.args.n_lm_grids ** 2 * num_dirs
        final_num_classes = num_classes
          
        if self.args.n_pre_classes is not None:
            num_classes = self.args.n_pre_classes
        else:
            num_classes = final_num_classes

        if self.args.pm_net == "none":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = None
        elif self.args.pm_net == "densenet121":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = densenet121(pretrained = self.args.use_pretrained, drop_rate = self.args.drop_rate)
            num_ftrs = self.perceptual_model.classifier.in_features # 1024
            self.perceptual_model.classifier = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "densenet169":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = densenet169(pretrained = self.args.use_pretrained, drop_rate = self.args.drop_rate)
            num_ftrs = self.perceptual_model.classifier.in_features # 1664
            self.perceptual_model.classifier = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "densenet201":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = densenet201(pretrained = self.args.use_pretrained, drop_rate = self.args.drop_rate)
            num_ftrs = self.perceptual_model.classifier.in_features # 1920
            self.perceptual_model.classifier = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "densenet161":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = densenet161(pretrained = self.args.use_pretrained, drop_rate = self.args.drop_rate)
            num_ftrs = self.perceptual_model.classifier.in_features # 2208
            self.perceptual_model.classifier = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "resnet18s":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet18s(pretrained=self.args.use_pretrained)
            num_ftrs = self.perceptual_model.fc.in_features
            self.perceptual_model.fc = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "resnet34s":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet34s(pretrained=self.args.use_pretrained)
            num_ftrs = self.perceptual_model.fc.in_features
            self.perceptual_model.fc = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "resnet50s":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet50s(pretrained=self.args.use_pretrained)
            num_ftrs = self.perceptual_model.fc.in_features
            self.perceptual_model.fc = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "resnet101s":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet101s(pretrained=self.args.use_pretrained)
            num_ftrs = self.perceptual_model.fc.in_features
            self.perceptual_model.fc = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "resnet152s":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet152s(pretrained=self.args.use_pretrained)
            num_ftrs = self.perceptual_model.fc.in_features
            self.perceptual_model.fc = nn.Linear(num_ftrs, num_classes)
        elif self.args.pm_net == "resnet18":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet18(num_classes = num_classes)
            num_ftrs = self.perceptual_model.fc.in_features
        elif self.args.pm_net == "resnet34":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet34(num_classes = num_classes)
            num_ftrs = self.perceptual_model.fc.in_features
        elif self.args.pm_net == "resnet50":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet50(num_classes = num_classes)
            num_ftrs = self.perceptual_model.fc.in_features
        elif self.args.pm_net == "resnet101":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet101(num_classes = num_classes)
            num_ftrs = self.perceptual_model.fc.in_features
        elif self.args.pm_net == "resnet152":
            self.map_rows = 224
            self.map_cols = 224
            self.perceptual_model = resnet152(num_classes = num_classes)
            num_ftrs = self.perceptual_model.fc.in_features # 2048
        else:
            raise Exception('pm-net required: resnet or densenet')


        if self.args.RL_type == 0:
            self.policy_model = policy_A3C(self.args.n_state_grids, 2+self.args.n_state_dirs, num_actions = self.args.num_actions)
        elif self.args.RL_type == 1:
            self.policy_model = policy_A3C(self.args.n_state_grids, 1+self.args.n_state_dirs, num_actions = self.args.num_actions)
        elif self.args.RL_type == 2:
            self.policy_model = policy_A3C(self.args.n_state_grids, 2*self.args.n_state_dirs, num_actions = self.args.num_actions, add_raw_map_scan = True)

        self.intri_model = intrinsic_model(self.grid_rows)

        ## D.P. was here ##

        if self.args.rl_model == "none":
            self.args.rl_model = None
        if self.args.pm_model == "none":
            self.args.pm_model = None
        
        # load models
        if self.args.pm_model is not None:
            state_dict = torch.load(self.args.pm_model)
            new_state_dict = OrderedDict()

            for k,v in state_dict.items():
                if 'module.' in k:
                    name = k[7:]
                else:
                    name = k
                new_state_dict[name] = v
            self.perceptual_model.load_state_dict(new_state_dict)
            print ('perceptual model %s is loaded.'%self.args.pm_model)


        if self.args.rl_model is not None:
            state_dict = torch.load(self.args.rl_model)
            new_state_dict = OrderedDict()
            for k,v in state_dict.items():
                if 'module.' in k:
                    name = k[7:]
                else:
                    name = k
                new_state_dict[name] = v
            self.policy_model.load_state_dict(new_state_dict)
            print ('policy model %s is loaded.'%self.args.rl_model)

        if self.args.ir_model is not None:
            self.intri_model.load_state_dict(torch.load(self.args.ir_model))
            print ('intri model %s is loaded.'%self.args.ir_model)

        # change n-classes
        if self.args.n_pre_classes is not None:
            # resize the output layer:
            new_num_classes = final_num_classes
            if "resnet" in self.args.pm_net:
                self.perceptual_model.fc = nn.Linear(self.perceptual_model.fc.in_features, new_num_classes, bias=True)
            elif "densenet" in args.pm_net:
                num_ftrs = self.perceptual_model.classifier.in_features
                self.perceptual_model.classifier = nn.Linear(num_ftrs, new_num_classes)
            print ('model: num_classes now changed to', new_num_classes)


        # data parallel, multi GPU
        # https://pytorch.org/tutorials/beginner/blitz/data_parallel_tutorial.html
        if self.device==torch.device("cuda") and torch.cuda.device_count()>0:
            print ("Use", torch.cuda.device_count(), 'GPUs')
            if self.perceptual_model != None:
                self.perceptual_model = nn.DataParallel(self.perceptual_model)
            self.policy_model = nn.DataParallel(self.policy_model)
            self.intri_model = nn.DataParallel(self.intri_model)
        else:
            print ("Use CPU")

        if self.perceptual_model != None:
            self.perceptual_model.to(self.device)
        self.policy_model.to(self.device)
        self.intri_model.to(self.device)
        # 

        if self.perceptual_model != None:
            if self.args.update_pm_by == "NONE":
                self.perceptual_model.eval()
            else:
                self.perceptual_model.train()

        if self.args.update_rl:
            self.policy_model.train()
        else:
            self.policy_model.eval()            

        self.min_scan_range, self.max_scan_range = self.args.scan_range #[0.1, 3.5]
        
        self.prob=np.zeros((1,3))
        self.values = []
        self.log_probs = []
        self.manhattans = []
        self.xyerrs = []
        self.manhattan = 0
        self.rewards = []
        self.intri_rewards = []
        self.reward = 0
        self.entropies = []
        self.gamma = 0.99
        self.tau = 0.95      #Are we sure?
        self.entropy_coef = self.args.c_entropy


        if self.args.update_pm_by == "NONE":
            self.optimizer_pm = None
        else:
            self.optimizer_pm = torch.optim.Adam(list(self.perceptual_model.parameters()), lr=self.args.lrpm)
            if self.args.schedule_pm:
                self.scheduler_pm = StepLR(self.optimizer_pm, step_size=self.args.pm_step_size, gamma=self.args.pm_decay)
                # self.scheduler_lp = ReduceLROnPlateau(self.optimizer_pm,
                #                                    factor = 0.5,
                #                                    patience = 2,
                #                                    verbose = True)
        models = []
        
        if self.args.update_pm_by=="RL" or self.args.update_pm_by=="BOTH":
            models = models + list(self.perceptual_model.parameters())
        if self.args.update_rl:
            models = models + list(self.policy_model.parameters())
        if self.args.update_ir:
            models = models + list(self.intri_model.parameters())

        if models==[]:
            self.optimizer = None
            print("WARNING: no model for RL")
        else:
            self.optimizer = torch.optim.Adam(models, lr=self.args.lrrl)
            if self.args.schedule_rl:
                self.scheduler_rl = StepLR(self.optimizer, step_size=self.args.rl_step_size, gamma=self.args.rl_decay)

        self.pm_backprop_cnt = 0
        self.rl_backprop_cnt = 0
        self.step_count = 0
        self.step_max = self.args.num[2]
        self.episode_count = 0
        self.acc_epi_cnt = 0
        self.episode_max = self.args.num[1]
        self.env_count = 0
        self.env_max = self.args.num[0]
        self.env_count = 0
        self.next_bin = 0
        self.done = False

        if self.args.verbose>0:
            print('maps, episodes, steps = %d, %d, %d'%(self.args.num[0], self.args.num[1], self.args.num[2]))
            
        self.cx = torch.zeros(1,256) #Variable(torch.zeros(1, 256))
        self.hx = torch.zeros(1,256) #Variable(torch.zeros(1, 256))
        self.max_grad_norm = 40

        map_side_len = 224 * self.args.map_pixel 
        self.xlim = (-0.5*map_side_len, 0.5*map_side_len)
        self.ylim = (-0.5*map_side_len, 0.5*map_side_len)
        self.xlim = np.array(self.xlim)
        self.ylim = np.array(self.ylim)

        self.map_width_meter = map_side_len

        # decide maze grids for each env
        # if self.args.maze_grids_range[0] == None:
        #     pass
        # else:
        #     self.n_maze_grids = np.random.randint(self.args.maze_grids_range[0],self.args.maze_grids_range[1])

        # self.hall_width = self.map_width_meter/self.n_maze_grids
        # if self.args.thickness == None:
        #     self.obs_radius = 0.25*self.hall_width
        # else:
        #     self.obs_radius = 0.5*self.args.thickness * self.hall_width

        self.collision_radius = self.args.collision_radius #0.25 # robot radius for collision

        self.longest = float(self.grid_dirs/2 + self.grid_rows-1 + self.grid_cols-1)  #longest possible manhattan distance

        self.cell_size = (self.xlim[1]-self.xlim[0])/self.grid_rows
        self.heading_resol = 2*np.pi/self.grid_dirs
        self.fwd_step_meters = self.cell_size*self.args.fwd_step
        self.collision = False
        self.collision_attempt = 0
        self.sigma_xy = self.args.sigma_xy # self.cell_size * 0.05
        
        self.cr_pixels = int(np.ceil(self.collision_radius / self.args.map_pixel))

        self.front_margin_pixels = int(np.ceil((self.collision_radius+self.fwd_step_meters) / self.args.map_pixel)) # how many pixels robot moves forward per step.
        self.side_margin_pixels = int(np.ceil(self.collision_radius / self.args.map_pixel))


        self.scans_over_map = np.zeros((self.grid_rows,self.grid_cols,360))


        self.scan_2d_low_tensor = torch.zeros((1,self.args.n_state_grids, self.args.n_state_grids),device=torch.device(self.device))
        self.map_for_LM = np.zeros((self.map_rows, self.map_cols))
        self.map_for_pose = np.zeros((self.grid_rows, self.grid_cols),dtype='float')
        self.map_for_RL = torch.zeros((1,self.args.n_state_grids, self.args.n_state_grids),device=torch.device(self.device))

        self.data_cnt = 0
        
        self.explored_space = np.zeros((self.grid_dirs,self.grid_rows, self.grid_cols),dtype='float')

        self.new_pose = False
        self.new_bel = False
        self.bel_list = []
        self.scan_list = []
        self.target_list = []

        self.likelihood = torch.ones((self.grid_dirs,self.grid_rows, self.grid_cols),
                                     device=torch.device(self.device), 
                                     dtype=torch.float)
        self.likelihood = self.likelihood / self.likelihood.sum()

        self.gt_likelihood = np.ones((self.grid_dirs,self.grid_rows,self.grid_cols))
        self.gt_likelihood_unnormalized = np.ones((self.grid_dirs,self.grid_rows,self.grid_cols))        
        
        self.belief = torch.ones((self.grid_dirs,self.grid_rows, self.grid_cols),device=torch.device(self.device))
        self.belief = self.belief / self.belief.sum()

        self.bel_ent = (self.belief * torch.log(self.belief)).sum().detach()
        # self.bel_ent = np.log(1.0/(self.grid_dirs*self.grid_rows*self.grid_cols))

        self.loss_likelihood = [] # loss for training PM model
        self.loss_ll=0
        
        self.loss_policy = 0
        self.loss_value = 0
        
        self.turtle_loc = np.zeros((self.map_rows,self.map_cols))

        self.policy_out = None
        self.value_out = None

        self.action_idx = -1
        self.action_from_policy = -1

        # what to do
        # current pose: where the robot really is. motion incurs errors in pose
        self.current_pose = Pose2d(0,0,0)
        self.live_pose = Pose2d(0,0,0)         # updated on-line from cbRobotPose
        self.odom_pose = Pose2d(0,0,0)  # updated from jay1/odom
        self.map_pose = Pose2d(0,0,0)  # updated from jay1/odom and transformed to map coordinates
        self.goal_pose = Pose2d(0,0,0)
        self.last_pose = Pose2d(0,0,0)
        self.perturbed_goal_pose = Pose2d(0,0,0)        
        self.start_pose = Pose2d(0,0,0)
        self.collision_pose = Pose2d(0,0,0)
        self.believed_pose = Pose2d(0,0,0)
        #grid pose
        self.true_grid = Grid(head=0,row=0,col=0)
        self.bel_grid = Grid(head=0,row=0,col=0)
        self.collision_grid = Grid(head=0,row=0,col=0)


        self.action_space = list(("turn_left", "turn_right", "go_fwd", "hold"))
        self.action_str = 'none'
        self.current_state = "new_env_pose"

        self.obj_act = None
        self.obj_rew = None
        self.obj_err = None
        self.obj_map = None
        self.obj_robot = None
        self.obj_heading = None
        self.obj_robot_bel = None        
        self.obj_heading_bel = None
        self.obj_pose = None
        self.obj_scan = None
        self.obj_gtl = None
        self.obj_lik = None
        self.obj_bel = None

        self.obj_bel_dist = None
        self.obj_gtl_dist = None
        self.obj_lik_dist = None

        self.obj_collision = None

        if self.args.save:
            home=os.environ['HOME']
            str_date_time = datetime.now().strftime('%Y%m%d-%H%M%S')
            # 1. try create /logs/YYMMDD-HHMMSS-00
            # 2. if exist create /logs/YYMMDD-HHMMSS-01, and so on
            i = 0
            dir_made=False
            while dir_made==False:
                self.log_dir = os.path.join(self.args.save_loc, str_date_time+'-%02d'%i)                
                try:
                    os.mkdir(self.log_dir)
                    dir_made=True
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
                    pass
                i=i+1

            if self.args.verbose > 0:
                print ('new directory %s'%self.log_dir)
                
            self.param_filepath = os.path.join(self.log_dir, 'param.txt')
            with open(self.param_filepath,'w+') as param_file:
                for arg in vars(self.args):
                    param_file.write('<%s=%s> '%(arg, getattr(self.args, arg)))
            if self.args.verbose > -1:
                print ('parameters saved at %s'%self.param_filepath)
            
            self.log_filepath = os.path.join(self.log_dir, 'log.txt')
            self.rollout_list = os.path.join(self.log_dir, 'rollout_list.txt')            
            self.pm_filepath = os.path.join(self.log_dir, 'perceptual.model')
            self.rl_filepath = os.path.join(self.log_dir, 'rl.model')
            self.ir_filepath = os.path.join(self.log_dir, 'ir.model')
            self.data_path = os.path.join(self.log_dir, 'data')
            self.fig_path =  os.path.join(self.log_dir, 'figures')
            # if self.args.save_data:
            try:
                os.mkdir(self.data_path)
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                pass
            
            if self.args.figure:
                try:
                    os.mkdir(self.fig_path)
                except OSError as exc:
                    if exc.errno != errno.EEXIST:
                        raise
                    pass

        
        self.twist_msg_move = Twist()
        self.twist_msg_move.linear.x = 0
        self.twist_msg_move.linear.y = 0
        self.twist_msg_move.angular.z = 0

        self.twist_msg_stop = Twist()
        self.twist_msg_stop.linear.x = 0
        self.twist_msg_stop.linear.y = 0
        self.twist_msg_stop.angular.z = 0
                
        #Subscribers
        #self.sub_map = rospy.Subscriber('map', OccupancyGrid, self.cbMap, queue_size = 1)
        # self.sub_odom = rospy.Subscriber('odom', Odometry, self.cbOdom, queue_size
        # = 1)
        self.dal_pose = PoseStamped()
        self.pose_seq_cnt = 0
        
        if self.args.gazebo:
            self.sub_laser_scan = rospy.Subscriber('scan', LaserScan, self.cbScan, queue_size = 1)
            self.pub_cmdvel = rospy.Publisher('cmd_vel', Twist, queue_size = 1)
            self.pub_dalpose = rospy.Publisher('dal_pose', PoseStamped, queue_size = 1)
            
            # self.sub_odom = rospy.Subscriber('odom', Odometry, self.cbOdom, queue_size = 1)
        elif self.args.jay1:
            self.pub_cmdvel = rospy.Publisher('jay1/cmd_vel', Twist, queue_size = 1)
            self.pub_dalpose = rospy.Publisher('dal_pose', PoseStamped, queue_size = 1)
            self.pub_dalscan = rospy.Publisher('dal_scan', LaserScan, queue_size = 1)
            self.pub_dalpath = rospy.Publisher('dal_path', MarkerArray)
            self.sub_laser_scan = rospy.Subscriber('jay1/scan', LaserScan, self.cbScanTop, queue_size = 1)
            self.sub_laser_scan = rospy.Subscriber('jay1/scan1', LaserScan, self.cbScanBottom, queue_size = 1)
            self.sub_robot_pose = rospy.Subscriber('jay1/robot_pose', PoseStamped, self.cbRobotPose, queue_size = 1)
            self.sub_odom = rospy.Subscriber('jay1/odom', Odometry, self.cbOdom, queue_size = 1)
            self.client = actionlib.SimpleActionClient('jay1/move_base', MoveBaseAction)
            self.client.wait_for_server()
            rospy.loginfo("Waiting for move_base action server...")
            wait = self.client.wait_for_server(rospy.Duration(5.0))
            if not wait:
                rospy.logerr("Action server not available!")
                rospy.signal_shutdown("Action server not available!")
                exit()
            rospy.loginfo("Connected to move base server")
            rospy.loginfo("Starting goals achievements ...")
        if self.args.gazebo or self.args.jay1:
            rospy.Timer(rospy.Duration(self.args.timer), self.loop_jay)

        self.max_turn_rate = 1.0
        self.turn_gain = 10
        self.fwd_err_margin = 0.005
        self.ang_err_margin = math.radians(0.2)

        self.fsm_state = "init"
        #end of init


    def loop(self): 

        if self.current_state == "new_env_pose":
            ### place objects in the env
            self.clear_objects()
            if self.args.load_map == None:
                self.set_maze_grid()
                self.set_walls()
            elif self.args.load_map == 'randombox':
                self.random_box()
            else:
                self.read_map()

            self.map_for_LM = fill_outer_rim(self.map_for_LM, self.map_rows, self.map_cols)
            if self.args.distort_map:
                self.map_for_LM = distort_map(self.map_for_LM, self.map_rows, self.map_cols)    

            self.make_low_dim_maps()

            if self.args.gtl_off == False:
                self.get_synth_scan_mp(self.scans_over_map, map_img=self.map_for_LM, xlim=self.xlim, ylim=self.ylim) # generate synthetic scan data over the map (and directions)

            self.reset_explored()
            if self.args.init_pose is not None:
                placed = self.set_init_pose()
            else:
                placed = self.place_turtle()
                
            if placed:
                self.current_state = "update_likelihood"
            else:
                print ("place turtle failed. trying a new map")
                return

            if self.args.figure==True:
                self.update_figure(newmap=True)

        elif self.current_state == "new_pose":
            self.reset_explored()

            if self.args.init_pose is not None:
                placed = self.set_init_pose()
            else:
                placed = self.place_turtle()

            self.current_state = "update_likelihood"
            

        elif self.current_state == "update_likelihood":
            self.get_lidar()
            
            self.update_explored()            

            
            if self.step_count == 0:
                self.save_roll_out = self.args.save & np.random.choice([False, True], p=[1.0-self.args.prob_roll_out, self.args.prob_roll_out])
                if self.save_roll_out:
                    #save roll-out for next episode.
                    self.roll_out_filepath = os.path.join(self.log_dir, 'roll-out-%03d-%03d.txt'%(self.env_count,self.episode_count))
                    print ('roll-out saving: %s'%self.roll_out_filepath)
            self.scan_2d, self.scan_2d_low = self.get_scan_2d_n_headings(self.scan_data, self.xlim, self.ylim)
            self.slide_scan()
            ### 2. update likelihood from observation

            self.compute_gtl(self.scans_over_map)

            if self.args.generate_data: # end the episode ... (no need for measurement/motion model)
                self.generate_data()
                if self.args.figure:             
                    self.update_figure()

                    plt.pause(1e-4)
                self.next_step()
                return

            self.likelihood = self.update_likelihood_rotate(self.map_for_LM, self.scan_2d)
            if self.args.mask:
                self.mask_likelihood()
            #self.likelihood.register_hook(print)
            ### z(t) = like x belief

            ### z(t) = like x belief
            # if self.collision == False:
            self.product_belief()

            ### reward r(t)
            self.update_bel_list()
            self.get_reward()

            ### action a(t) given s(t) = (z(t)|Map)
            if self.args.verbose>0:          
                self.report_status(end_episode=False)
            if self.save_roll_out:
                self.collect_data()
            if self.args.figure:             
                self.update_figure()

            if self.step_count >= self.step_max-1:
                self.run_action_module(no_update_fig=True)
                self.skip_to_end = True
            else:
                self.run_action_module()

            if self.skip_to_end:
                self.skip_to_end = False
                self.next_ep()
                return
            
            ### environment: set target
            self.update_target_pose()

            
            # do the rest: ation, trans-belief, update gt
            self.collision_check()
            self.execute_action_teleport()

            ### environment: change belief z_hat(t+1)
            self.transit_belief()

            ### increase time step
            # self.update_current_pose()

            
            if self.collision == False:
                self.update_true_grid()
            


            self.next_step()
            return

        else:
            print("undefined state name %s"%self.current_state)
            self.current_state = None
            exit()

        return



        

    def get_statistics(self, dis, name):
        DIRS = 'NWSE'
        this=[]

        for i in range(self.grid_dirs):
            # this.append('%s(%s%1.3f,%s%1.3f,%s%1.3f%s)'\
            #             %(DIRS[i], bcolors.WARNING,100*dis[i,:,:].max(),
            #               bcolors.OKGREEN,100*dis[i,:,:].median(),
            #               bcolors.FAIL,100*dis[i,:,:].min(),bcolors.ENDC))
            this.append(' %s(%1.2f,%1.2f,%1.2f)'\
                        %(DIRS[i], 100*dis[i,:,:].max(),
                          100*dis[i,:,:].median(),
                          100*dis[i,:,:].min()))
        return name+':%19s|%23s|%23s|%23s|'%tuple(this[th] for th in range(self.grid_dirs))

    def circular_placement(self, x, n):
        width = x.shape[2]
        height = x.shape[1]
        N = (n//2+1)*max(width,height)
        img = np.zeros((N,N))
        for i in range(n):
            if i < n//4:
                origin = (i, (n//4-i))
            elif i < 2*n//4:
                origin = (i, (i-n//4))
            elif i < 3*n//4:
                origin = (n-i, (i-n//4))
            else:
                origin = (n-i, n+n//4-i)

            ox = origin[0]*height
            oy = origin[1]*width

            img[ox:ox+height, oy:oy+width] = x[i,:,:]
        return img
        
    # def square_clock(self, x, n):
    #     width = x.shape[2]
    #     height = x.shape[1]
    #     quater = n//4-1

    #     #even/odd
    #     even = 1 - quater % 2
    #     side = quater+2+even
    #     N = side*max(width,height)
    #     img = np.zeros((N,N))
        
    #     for i in range(n):
    #         s = (i+n//8)%n
    #         if s < n//4:
    #             org = (0, n//4-s)
    #         elif s < n//2:
    #             org = (s-n//4+even, 0)
    #         elif s < 3*n//4:
    #             org = (n//4+even, s-n//2+even)
    #         else:
    #             org = (n//4-(s-3*n//4), n//4+even)
    #         ox = org[0]*height
    #         oy = org[1]*width
    #         img[ox:ox+height, oy:oy+width] = x[i,:,:]
    #     del x
    #     return img, side

    def draw_compass(self, ax):
        cx = 0.9 * self.xlim[1]
        cy = 0.9 * self.ylim[0]

        lengthNS = self.xlim[1] * 0.1
        lengthEW = self.ylim[1] * 0.075        

        theta = - self.current_pose.theta
        Nx = cx + lengthNS * np.cos(theta)
        Ny = cy + lengthNS* np.sin(theta)
        Sx = cx + lengthNS * np.cos(theta+np.pi)
        Sy = cy + lengthNS * np.sin(theta+np.pi)
        Ni = to_index(Nx, self.map_rows, self.xlim)
        Nj = to_index(Ny, self.map_cols, self.ylim)
        Si = to_index(Sx, self.map_rows, self.xlim)
        Sj = to_index(Sy, self.map_cols, self.ylim)

        Ex = cx + lengthEW * np.cos(theta-np.pi/2)
        Ey = cy + lengthEW * np.sin(theta-np.pi/2)
        Wx = cx + lengthEW * np.cos(theta+np.pi/2)
        Wy = cy + lengthEW * np.sin(theta+np.pi/2)
        Ei = to_index(Ex, self.map_rows, self.xlim)
        Ej = to_index(Ey, self.map_cols, self.ylim)
        Wi = to_index(Wx, self.map_rows, self.xlim)
        Wj = to_index(Wy, self.map_cols, self.ylim)
        xdata = Sj, Nj, Wj, Ej
        ydata = Si, Ni, Wi, Ei

        if hasattr(self, 'obj_compass1'):
            self.obj_compass1.update({'xdata':xdata, 'ydata':ydata})
        else:
            self.obj_compass1, = ax.plot(xdata, ydata, 'r', alpha = 0.5)


    def draw_center(self, ax):
        x = to_index(0, self.map_rows, self.xlim)
        y = to_index(0, self.map_cols, self.ylim)
        # radius = self.map_rows*0.4/self.grid_rows
        radius = self.cr_pixels # self.collision_radius / (self.xlim[1]-self.xlim[0]) * self.map_rows
        theta = 0-np.pi/2
        xdata = y, y+radius*3*np.cos(theta)
        ydata = x, x+radius*3*np.sin(theta)

        obj_robot = Wedge((y,x), radius, 0, 360, color='r',alpha=0.5)
        obj_heading, = ax.plot(xdata, ydata, 'r', alpha=0.5) 
        ax.add_artist(obj_robot)


    def draw_collision(self, ax, collision):
        if collision == False:
            if self.obj_collision == None:
                return
            else:
                self.obj_collision.update({'visible':False})
        else:
            x = to_index(self.collision_pose.x, self.map_rows, self.xlim)
            y = to_index(self.collision_pose.y, self.map_cols, self.ylim)
            radius = self.cr_pixels #self.collision_radius / (self.xlim[1]-self.xlim[0]) * self.map_rows

            if self.obj_collision == None:
                self.obj_collision = Wedge((y,x), radius, 0, 360, color='y',alpha=0.5, visible=True)
                ax.add_artist(self.obj_collision)
            else:
                self.obj_collision.update({'center': [y,x], 'visible':True})

            # self.obj_robot.set_data(self.turtle_loc)
            # plt.pause(0.01)

    def draw_robot(self, ax):
        x = to_index(self.current_pose.x, self.map_rows, self.xlim)
        y = to_index(self.current_pose.y, self.map_cols, self.ylim)
        # radius = self.map_rows*0.4/self.grid_rows
        radius = self.cr_pixels # self.collision_radius / (self.xlim[1]-self.xlim[0]) * self.map_rows
        theta = -self.current_pose.theta-np.pi/2
        xdata = y, y+radius*3*np.cos(theta)
        ydata = x, x+radius*3*np.sin(theta)

        if self.obj_robot == None:
            #self.obj_robot = ax.imshow(self.turtle_loc, alpha=0.5, cmap=plt.cm.binary)
            # self.obj_robot = ax.imshow(self.turtle_loc, alpha=0.5, cmap=plt.cm.Reds,interpolation='nearest')
            self.obj_robot = Wedge((y,x), radius, 0, 360, color='r',alpha=0.5)
            self.obj_heading, = ax.plot(xdata, ydata, 'r', alpha=0.5) 
            ax.add_artist(self.obj_robot)
        else:
            self.obj_robot.update({'center': [y,x]})
            self.obj_heading.update({'xdata':xdata, 'ydata':ydata})
            # self.obj_robot.set_data(self.turtle_loc)
            # plt.pause(0.01)


    def update_believed_pose(self):
        o_bel,i_bel,j_bel = np.unravel_index(np.argmax(self.belief.cpu().detach().numpy(), axis=None), self.belief.shape)
        x_bel = to_real(i_bel, self.xlim,self.grid_rows)
        y_bel = to_real(j_bel, self.ylim,self.grid_cols)
        theta = o_bel * self.heading_resol
        self.believed_pose.x = x_bel
        self.believed_pose.y = y_bel
        self.believed_pose.theta = theta
        self.publish_dal_pose(x_bel, y_bel, theta)

        
    def publish_dal_pose(self,x,y,theta):
        self.dal_pose.pose.position.x = -y
        self.dal_pose.pose.position.y = x
        self.dal_pose.pose.position.z = 0
        quatern = quaternion_from_euler(0,0, theta+np.pi/2)
        self.dal_pose.pose.orientation.x=quatern[0]
        self.dal_pose.pose.orientation.y=quatern[1]
        self.dal_pose.pose.orientation.z=quatern[2]
        self.dal_pose.pose.orientation.w=quatern[3]
        self.dal_pose.header.frame_id = 'map'
        self.dal_pose.header.seq = self.pose_seq_cnt
        self.pose_seq_cnt += 1
        self.pub_dalpose.publish(self.dal_pose)

    def update_map_T_odom(self):
        map_pose = (self.believed_pose.x, self.believed_pose.y, self.believed_pose.theta)
        odom_pose = (self.odom_pose.x, self.odom_pose.y, self.odom_pose.theta)
        self.map_T_odom = define_tf(map_pose, odom_pose)


    def draw_bel(self, ax):
        o_bel,i_bel,j_bel = np.unravel_index(np.argmax(self.belief.cpu().detach().numpy(), axis=None), self.belief.shape)
        x_bel = to_real(i_bel, self.xlim,self.grid_rows)
        y_bel = to_real(j_bel, self.ylim,self.grid_cols)
        x = to_index(x_bel, self.map_rows, self.xlim)
        y = to_index(y_bel, self.map_cols, self.ylim)
        # radius = self.map_rows*0.4/self.grid_rows
        radius = self.cr_pixels # self.collision_radius / (self.xlim[1]-self.xlim[0]) * self.map_rows
        theta = o_bel * self.heading_resol
        theta = -theta-np.pi/2
        xdata = y, y+radius*3*np.cos(theta)
        ydata = x, x+radius*3*np.sin(theta)

        if self.obj_robot_bel == None:
            #self.obj_robot = ax.imshow(self.turtle_loc, alpha=0.5, cmap=plt.cm.binary)
            # self.obj_robot = ax.imshow(self.turtle_loc, alpha=0.5, cmap=plt.cm.Reds,interpolation='nearest')
            self.obj_robot_bel = Wedge((y,x), radius*0.95, 0, 360, color='b',alpha=0.5)
            self.obj_heading_bel, = ax.plot(xdata, ydata, 'b', alpha=0.5) 
            ax.add_artist(self.obj_robot_bel)
        else:
            self.obj_robot_bel.update({'center': [y,x]})
            self.obj_heading_bel.update({'xdata':xdata, 'ydata':ydata})

    def init_figure(self):
        self.init_fig = True
        if self.args.figure == True:# and self.obj_fig==None:
            self.obj_fig = plt.figure(figsize=(16,12))
            plt.set_cmap('viridis')

            self.gridspec = gridspec.GridSpec(3,5)
            self.ax_map = plt.subplot(self.gridspec[0,0])
            self.ax_scan = plt.subplot(self.gridspec[1,0])
            self.ax_pose =  plt.subplot(self.gridspec[2,0])

            self.ax_bel =  plt.subplot(self.gridspec[0,1])
            self.ax_lik =  plt.subplot(self.gridspec[1,1])
            self.ax_gtl =  plt.subplot(self.gridspec[2,1])


            self.ax_pbel =  plt.subplot(self.gridspec[0,2:4])
            self.ax_plik =  plt.subplot(self.gridspec[1,2:4])
            self.ax_pgtl =  plt.subplot(self.gridspec[2,2:4])

            self.ax_act = plt.subplot(self.gridspec[0,4])
            self.ax_rew = plt.subplot(self.gridspec[1,4])
            self.ax_err = plt.subplot(self.gridspec[2,4])

            plt.subplots_adjust(hspace = 0.4, wspace=0.4, top=0.95, bottom=0.05)


    def update_figure(self, newmap=False):
        if self.init_fig==False:
            self.init_figure()
        
        if newmap:
            ax=self.ax_map
            if self.obj_map == None:
                # self.ax_map = ax
                self.obj_map = ax.imshow(self.map_for_LM, cmap=plt.cm.binary,interpolation='nearest')
                ax.grid()
                ticks = np.linspace(0,self.map_rows,self.grid_rows,endpoint=False)
                ax.set_yticks(ticks)
                ax.set_xticks(ticks)
                ax.tick_params(axis='y', labelleft='off')
                ax.tick_params(axis='x', labelbottom='off')
                ax.tick_params(bottom="off", left="off")
            else:
                self.obj_map.set_data(self.map_for_LM)
            self.draw_robot(ax)
            return

        ax=self.ax_map 
        self.draw_robot(ax)
        self.draw_bel(ax)
        self.draw_collision(ax, self.collision)

        ax=self.ax_scan 

        if self.obj_scan == None:
            self.obj_scan = ax.imshow(self.scan_2d[0,:,:], cmap = plt.cm.binary,interpolation='gaussian')
            self.obj_scan_slide = ax.imshow(self.scan_2d_slide[:,:], cmap = plt.cm.Blues,interpolation='gaussian', alpha=0.5)
            # self.obj_scan_low = ax.imshow(cv2.resize(1.0*self.scan_2d_low[:,:], (self.map_rows, self.map_cols), interpolation=cv2.INTER_NEAREST), cmap = plt.cm.binary,interpolation='nearest', alpha=0.5)
            self.draw_center(ax)
            self.draw_compass(ax)
            ax.set_title('LiDAR Scan')
        else:
            self.obj_scan.set_data(self.scan_2d[0,:,:])
            # self.obj_scan_low.set_data(cv2.resize(1.0*self.scan_2d_low[:,:], (self.map_rows, self.map_cols), interpolation=cv2.INTER_NEAREST))
            self.obj_scan_slide.set_data(self.scan_2d_slide[:,:])
            self.draw_compass(ax)

        ax=self.ax_pose 
        self.update_pose_plot(ax)

        ## GTL ##
        if self.args.gtl_off:
            pass
        else:
            ax=self.ax_gtl 
            self.update_gtl_plot(ax)

        ## BELIEF ##
        ax=self.ax_bel 
        self.update_belief_plot(ax)


        ## LIKELIHOOD ##
        ax=self.ax_lik 
        self.update_likely_plot(ax)
        ax=self.ax_pbel 
        self.update_bel_dist(ax)
        ax=self.ax_pgtl 
        self.update_gtl_dist(ax)
        ax=self.ax_plik 
        self.update_lik_dist(ax)

        # show last step, and save
        if self.step_count >= self.step_max-1:
            self.ax_map.set_title('action(%d):%s'%(self.step_count,""))
            # self.prob = np.array([0,0,0])
            # self.action_from_policy=-1
            self.clear_act_dist(self.ax_act)
            act_lttr=['L','R','F','-']
            self.obj_rew= self.update_list(self.ax_rew,self.rewards,self.obj_rew,"Reward", text=act_lttr[self.action_idx])
            self.obj_err = self.update_list(self.ax_err,self.xyerrs,self.obj_err,"Error")
            plt.pause(1e-4)
            self.save_figure()


    def save_figure(self):
        if self.args.save and self.acc_epi_cnt % self.args.figure_save_freq == 0:
            figname=os.path.join(self.fig_path,'%03d-%03d-%03d.png'%(self.env_count,
                                                                         self.episode_count,
                                                                         self.step_count))
            plt.savefig(figname)
            if self.args.verbose > 1:
                print (figname)


    def update_pose_plot(self, ax):

        pose = np.zeros((self.grid_rows,self.grid_cols,3))
        pose[:,:,0] = 1-self.map_for_pose
        pose[:,:,1] = 1-self.map_for_pose
        pose[:,:,2] = 1-self.map_for_pose

        if (pose[self.true_grid.row, self.true_grid.col,:] == [0, 0, 0]).all():
            pose[self.true_grid.row, self.true_grid.col, :] = [0.5, 0, 0]
            # pose[self.true_grid.row, self.true_grid.col, 2] = [0.5, 0, 0]
        elif (pose[self.true_grid.row, self.true_grid.col,:] == [1, 1, 1]).all():
            pose[self.true_grid.row, self.true_grid.col, :] = [1.0, 0, 0]

        if (pose[self.bel_grid.row, self.bel_grid.col, :] == [0,0,0]).all():
            pose[self.bel_grid.row, self.bel_grid.col, :] = [0,0,0.5]
        elif (pose[self.bel_grid.row, self.bel_grid.col, :] == [1,1,1]).all():
            pose[self.bel_grid.row, self.bel_grid.col, :] = [0,0,1]
        elif (pose[self.bel_grid.row, self.bel_grid.col, :] == [1,0,0]).all():
            pose[self.bel_grid.row, self.bel_grid.col, :] = [.5,0,.5]
        elif (pose[self.bel_grid.row, self.bel_grid.col, :] == [0.5,0,0]).all():
            pose[self.bel_grid.row, self.bel_grid.col, :] = [0.25,0,0.25]

        if self.collision:
            pose[min(self.grid_rows-1, max(0, self.collision_grid.row)), min(self.grid_cols-1, max(0, self.collision_grid.col)),:] = [0.5, 0.5, 0]
        if self.obj_pose == None:
            self.obj_pose = ax.imshow(pose, cmap = plt.cm.binary,interpolation='nearest')
            ax.grid()
            ax.set_yticks(np.arange(0,self.grid_rows)-0.5)
            ax.set_xticks(np.arange(0,self.grid_cols)-0.5)
            ax.tick_params(axis='y', labelleft='off')
            ax.tick_params(axis='x', labelbottom='off')
            ax.tick_params(bottom="off", left="off")
            ax.set_title("Occupancy Grid")
        else:
            self.obj_pose.set_data(pose)


    def update_likely_plot(self,ax):
        lik = self.likelihood.cpu().detach().numpy()
        # if lik.min() == lik.max():
        #     lik *= 0
        # lik -= lik.min()
        # lik /= lik.max()
        lik, side = square_clock(lik, self.grid_dirs)
        # lik=self.circular_placement(lik, self.grid_dirs)
        # lik = lik.reshape(self.grid_rows*self.grid_dirs,self.grid_cols) 
        # lik = np.swapaxes(lik,0,1)
        # lik = lik.reshape(self.grid_rows, self.grid_dirs*self.grid_cols)
        # lik = np.concatenate((lik[0,:,:],lik[1,:,:],lik[2,:,:],lik[3,:,:]), axis=1)
        if self.obj_lik == None:
            self.obj_lik = ax.imshow(lik,interpolation='nearest')
            ax.grid()
            ticks = np.linspace(0,self.grid_rows*side, side,endpoint=False)-0.5
            ax.set_yticks(ticks)
            ax.set_xticks(ticks)
            ax.tick_params(axis='y', labelleft='off')
            ax.tick_params(axis='x', labelbottom='off')
            ax.tick_params(bottom="off", left="off")
            ax.set_title('Likelihood from NN')
        else:
            self.obj_lik.set_data(lik)
        self.obj_lik.set_norm(norm = cm.Normalize().autoscale(lik))

    def update_act_dist(self, ax):
        y = self.prob.flatten()
        if self.obj_act == None:
            x = range(y.size)
            self.obj_act = ax.bar(x,y)
            ax.set_ylim([0, 1.1])
            ax.set_title("Action PDF")
            ax.set_xticks(np.array([0,1,2]))
            ax.set_xticklabels(('L','R','F'))
            self.obj_act_act = None
        else:
            for bar,a in zip(self.obj_act, y):
                bar.set_height(a)
        if self.obj_act_act == None :
            if self.action_from_policy is not -1:
                z = y[min(self.action_from_policy,2)]
                self.obj_act_act = ax.text(self.action_from_policy, z, '*')
        else:
            if self.action_from_policy is not -1:
                z = y[min(self.action_from_policy,2)]
                self.obj_act_act.set_position((self.action_from_policy, z))

    def clear_act_dist(self, ax):
        ax.clear()
        if self.obj_act==None:
            pass
        else:
            self.obj_act = None

        if self.obj_act_act == None:
            pass
        else:
            self.obj_act_act = None

            
    def update_list(self,ax,y,obj,title, text=None):
        # y = self.rewards
        x = range(len(y))
        if obj == None:
            obj, = ax.plot(x,y,'.-')
            ax.set_title(title)
        else:
            obj.set_ydata(y)
            obj.set_xdata(x)
            if text is not None:
                ax.text(x[-1],y[-1], text)
            # recompute the ax.dataLim
            ax.relim()
            # update ax.viewLim using the new dataLim
            ax.autoscale_view()
        return obj

    def update_bel_dist(self,ax):
        y = (self.belief.cpu().detach().numpy().flatten())
        gt = np.zeros_like(self.belief.cpu().detach().numpy())
        gt[self.true_grid.head, self.true_grid.row, self.true_grid.col] = 1
        gt = gt.flatten()
        gt_x = np.argmax(gt)
        if self.obj_bel_dist == None:
            x = range(y.size)
            self.obj_bel_dist, = ax.plot(x,y,'.')
            self.obj_bel_max, = ax.plot(np.argmax(y), np.max(y), 'x', color='r', label='bel')
            self.obj_gt_bel, = ax.plot(gt_x, y[gt_x], '^', color='r', label='gt')
            ax.legend()
            self.obj_bel_val = ax.text(np.argmax(y), np.max(y), "%f"%np.max(y))
            ax.set_ylim([0, y.max()*2])
            # ax.set_ylabel('Belief')
            # ax.set_xlabel('Pose')
            ax.set_title("Belief")
        else:
            self.obj_bel_dist.set_ydata(y)
            self.obj_bel_max.set_xdata(np.argmax(y))
            self.obj_bel_max.set_ydata(np.max(y))
            self.obj_gt_bel.set_xdata(gt_x)
            self.obj_gt_bel.set_ydata(y[gt_x])

            self.obj_bel_val.set_position((np.argmax(y), np.max(y)))
            self.obj_bel_val.set_text("%f"%np.max(y))
            ax.set_ylim([0, y.max()*2])

    def update_gtl_dist(self,ax):
        # y = (self.gt_likelihood.cpu().detach().numpy().flatten())
        y = self.gt_likelihood.flatten()
        if self.obj_gtl_dist == None:
            x = range(y.size)
            self.obj_gtl_dist, = ax.plot(x,y,'.')
            self.obj_gtl_max, = ax.plot(np.argmax(y), np.max(y), 'rx')
            ax.set_ylim([0, y.max()*2])
            # ax.set_ylabel('GTL')
            # ax.set_xlabel('Pose')
            ax.set_title("GTL")
        else:
            self.obj_gtl_dist.set_ydata(y)
            self.obj_gtl_max.set_ydata(np.max(y))
            self.obj_gtl_max.set_xdata(np.argmax(y))
            ax.set_ylim([0, y.max()*2])

    def update_lik_dist(self,ax):
        y = (self.likelihood.cpu().detach().numpy().flatten())
        if self.obj_lik_dist == None:
            x = range(y.size)
            self.obj_lik_dist, = ax.plot(x,y,'.')
            self.obj_lik_max, = ax.plot(np.argmax(y), np.max(y), 'rx')
            ax.set_ylim([0, y.max()*2])
            # ax.set_ylabel('Likelihood')
            # ax.set_xlabel('Pose')
            ax.set_title("Likelihood")
        else:
            self.obj_lik_dist.set_ydata(y)
            self.obj_lik_max.set_ydata(np.max(y))
            self.obj_lik_max.set_xdata(np.argmax(y))
            ax.set_ylim([0, y.max()*2])

    def update_belief_plot(self,ax):
        bel = self.belief.cpu().detach().numpy()
        # if bel.min() == bel.max():
        #     bel *= 0
        # bel -= bel.min()
        # bel /= bel.max()
        bel,side = square_clock(bel, self.grid_dirs)
        #bel=self.circular_placement(bel, self.grid_dirs)
        # bel = bel.reshape(self.grid_rows*self.grid_dirs,self.grid_cols) 
        # bel = np.swapaxes(bel,0,1)
        # bel = bel.reshape(self.grid_rows,self.grid_dirs*self.grid_cols) 
        # bel = np.concatenate((bel[0,:,:],bel[1,:,:],bel[2,:,:],bel[3,:,:]), axis=1)
        if self.obj_bel == None:
            self.obj_bel = ax.imshow(bel,interpolation='nearest')
            ax.grid()
            ticks = np.linspace(0,self.grid_rows*side, side,endpoint=False)-0.5
            ax.set_yticks(ticks)
            ax.set_xticks(ticks)
            ax.tick_params(axis='y', labelleft='off')
            ax.tick_params(axis='x', labelbottom='off')
            ax.tick_params(bottom="off", left="off")
            ax.set_title('Belief (%.3f)'%self.belief.cpu().detach().numpy().max())
            
        else:
            self.obj_bel.set_data(bel)
            ax.set_title('Belief (%.3f)'%self.belief.cpu().detach().numpy().max())

        self.obj_bel.set_norm(norm = cm.Normalize().autoscale(bel))




    def update_gtl_plot(self,ax):
        # gtl = self.gt_likelihood.cpu().detach().numpy()
        gtl = self.gt_likelihood
        gtl, side = square_clock(gtl, self.grid_dirs)
        if self.obj_gtl == None:
            self.obj_gtl = ax.imshow(gtl,interpolation='nearest')
            ax.grid()
            ticks = np.linspace(0,self.grid_rows*side, side,endpoint=False)-0.5
            ax.set_yticks(ticks)
            ax.set_xticks(ticks)
            ax.tick_params(axis='y', labelleft='off')
            ax.tick_params(axis='x', labelbottom='off')
            ax.tick_params(bottom="off", left="off")
            ax.set_title('Target Likelihood')
        else:
            self.obj_gtl.set_data(gtl)
        self.obj_gtl.set_norm(norm = cm.Normalize().autoscale(gtl))


    def report_status(self,end_episode=False):
        if end_episode:
            reward = sum(self.rewards)
            loss = self.loss_ll #sum(self.loss_likelihood)
            dist = sum(self.manhattans)
        else:
            reward = self.rewards[-1]
            loss = self.loss_ll
            dist = self.manhattan
        eucl = self.get_euclidean()
        
        if self.optimizer == None:
            lr_rl = 0
        else:
            lr_rl = self.optimizer.param_groups[0]['lr']
        if self.optimizer_pm == None:
            lr_pm = 0
        else:
            lr_pm = self.optimizer_pm.param_groups[0]['lr']

        if self.args.save:
            with open(self.log_filepath,'a') as flog:
                flog.write('%d %d %d %f %f %f %f %f %f %f %f %e %e %f %f %f %f\n'%(self.env_count, self.episode_count,self.step_count,
                                                                                   loss, dist, reward,
                                                                                   self.loss_policy, self.loss_value, 
                                                                                   self.prob[0,0],self.prob[0,1],self.prob[0,2],
                                                                                   lr_rl,
                                                                                   lr_pm,
                                                                                   eucl,
                                                                                   self.action_time,
                                                                                   self.gtl_time,
                                                                                   self.lm_time
                                                                                   
                                                                   ))
        print('%d %d %d %f %f %f %f %f %f %f %f %e %e %f %f %f %f'%(self.env_count, self.episode_count,self.step_count,
                                                        loss, dist, reward,
                                                        self.loss_policy, self.loss_value, 
                                                        self.prob[0,0],self.prob[0,1],self.prob[0,2],
                                                        lr_rl,
                                                           lr_pm,
                                                           eucl,
                                                           self.action_time,
                                                           self.gtl_time,
                                                           self.lm_time
                                                    ))

    def process_link_state(self, pose):
        return np.array([
            pose.position.x,
            pose.position.y,
            pose.position.z,
            pose.orientation.x,
            pose.orientation.y,
            pose.orientation.z,
            pose.orientation.w
            ])

    def process_model_state(self, pose):
        return np.array([
            pose.position.x,
            pose.position.y,
            pose.position.z,
            pose.orientation.x,
            pose.orientation.y,
            pose.orientation.z,
            pose.orientation.w
            ])


    def update_current_pose_from_gazebo(self):
        rospy.wait_for_service('/gazebo/get_model_state')
        loc = self.get_model_state(self.robot_model_name,'')

        qtn=loc.pose.orientation
        roll,pitch,yaw=quaternion_to_euler_angle(qtn.w, qtn.x, qtn.y, qtn.z)
        self.current_pose = Pose2d(theta=yaw, x=loc.pose.position.x, y=loc.pose.position.y)


    def update_current_pose_from_robot(self):
        self.current_pose.x = self.live_pose.x
        self.current_pose.y = self.live_pose.y
        self.current_pose.theta = self.live_pose.theta
        

    def update_true_grid(self):
        self.true_grid.row=to_index(self.current_pose.x, self.grid_rows, self.xlim)
        self.true_grid.col=to_index(self.current_pose.y, self.grid_cols, self.ylim)
        heading = self.current_pose.theta
        
        self.true_grid.head = self.grid_dirs * wrap(heading + np.pi/self.grid_dirs) / 2.0 / np.pi
        self.true_grid.head = int(self.true_grid.head % self.grid_dirs)


    def sync_goal_to_true_grid(self):
        self.perturbed_goal_pose.x = to_real(self.true_grid.row, self.xlim, self.grid_rows)
        self.perturbed_goal_pose.y = to_real(self.true_grid.col, self.ylim, self.grid_cols)
        self.perturbed_goal_pose.theta = self.heading_resol*self.true_grid.head
    
    def sync_goal_to_current(self):

        self.goal_pose.x = self.current_pose.x 
        self.goal_pose.y = self.current_pose.y 
        self.goal_pose.theta = self.current_pose.theta
        self.perturbed_goal_pose.x = self.current_pose.x 
        self.perturbed_goal_pose.y = self.current_pose.y 
        self.perturbed_goal_pose.theta = self.current_pose.theta


    def init_motion_control(self):
        # self.start_pose = self.believed_pose
        self.t_motion_init = time.time()
        # self.wait_for_scan = True
        return


    def do_motion_control(self):

        start_pose = self.believed_pose
        goal_pose = self.perturbed_goal_pose # soft copy !

        odom_pose = (self.odom_pose.x, self.odom_pose.y, self.odom_pose.theta) # update from cbOdom
        odom_T_obs = tuple_to_hg(odom_pose)
        map_T_obs = np.dot(self.map_T_odom, odom_T_obs)
        map_pose = np.array(hg_to_tuple(map_T_obs), np.float32)
        self.map_pose.x = map_pose[0]
        self.map_pose.y = map_pose[1]
        self.map_pose.theta = map_pose[2]
        self.publish_dal_pose(self.map_pose.x, self.map_pose.y, self.map_pose.theta)
        
        t_elapse = time.time() - self.t_motion_init

        done = False
        fwd_err = 0
        lat_err = 0
        ang_err = 0
        if self.action_str == "hold":
            done = True
        elif self.action_str == "go_fwd":
            # go 1 step fwd
            fwd_check = self.fwd_clear() & self.fwd_clear_bottom()   
            if fwd_check == False:
                self.pub_cmdvel.publish(self.twist_msg_stop)
                rospy.loginfo("Forward is Not Clear")
                done = True
            else:
                p_start = np.array([start_pose.x, start_pose.y])
                p_goal = np.array([goal_pose.x, goal_pose.y])
                p_now = np.array([self.map_pose.x, self.map_pose.y])
                yaw_now = self.map_pose.theta
                fwd_err,lat_err,ang_err = transform(p_start,p_goal,p_now, yaw_now)
                if fwd_err > - self.fwd_err_margin: # done
                    self.pub_cmdvel.publish(self.twist_msg_stop)
                    done = True
                else: #go
                    fwd_vel, ang_vel = control_law(fwd_err, lat_err, ang_err, t_elapse)
                    self.twist_msg_move.linear.x = fwd_vel
                    self.twist_msg_move.linear.y = 0
                    self.twist_msg_move.angular.z = ang_vel
                    self.pub_cmdvel.publish(self.twist_msg_move)
                    done = False

        elif self.action_str == "turn_left" or self.action_str == "turn_right":
            # turn
            # measure orientation error:
            ang_err = wrap(goal_pose.theta - self.map_pose.theta)
            fwd_err = 0
            lat_err = 0
            if np.abs(ang_err) > self.ang_err_margin:
                fwd_vel, ang_vel = control_law(fwd_err, lat_err, -ang_err, t_elapse)
                # ang_vel = np.clip(self.turn_gain*ang_err, -self.max_turn_rate, self.max_turn_rate)
                self.twist_msg_move.linear.x = fwd_vel
                self.twist_msg_move.linear.y = 0
                self.twist_msg_move.angular.z = ang_vel
                self.pub_cmdvel.publish(self.twist_msg_move)
                done = False
            else:
                self.pub_cmdvel.publish(self.twist_msg_stop)
                done = True

        if self.args.verbose > 1:
            print ("fwd err: %.3f, lat err: %.3f, ang err(deg): %.2f"%(
                fwd_err, lat_err, math.degrees(ang_err)))

        return not done
    
    def teleport_turtle(self):
        if self.args.verbose>1: print("inside turtle teleportation")
        # if self.args.perturb > 0:
        self.current_pose.x = self.perturbed_goal_pose.x
        self.current_pose.y = self.perturbed_goal_pose.y
        self.current_pose.theta = self.perturbed_goal_pose.theta

    #     pose = self.turtle_pose_msg
    #     twist = self.turtle_twist_msg

    #     msg = ModelState()
    #     msg.model_name = self.robot_model_name
    #     msg.pose = pose
    #     msg.twist = twist

    #     if self.args.verbose > 1:
    #         print("teleport target = %f,%f"%(msg.pose.position.x, msg.pose.position.y))
    #     rospy.wait_for_service('/gazebo/set_model_state')
    #     resp = self.set_model_state(msg)

    #     while True:
    #         rospy.wait_for_service("/gazebo/get_model_state")
    #         loc = self.get_model_state(self.robot_model_name,'')
    #         if np.abs(self.process_model_state(loc.pose) - self.process_model_state(msg.pose)).sum():
    #             break
        
    #     if self.args.verbose > 1:
    #         print("teleport result  = %f,%f"%(loc.pose.position.x, loc.pose.position.y))

    def set_maze_grid(self):
        # decide maze grids for each env
        # if self.args.maze_grids_range[0] == None:
        #     pass
        # else:

        self.n_maze_grids = np.random.choice(self.args.n_maze_grids)

        self.hall_width = self.map_width_meter/self.n_maze_grids
        if self.args.thickness == None:
            self.obs_radius = 0.25*self.hall_width
        else:
            self.obs_radius = 0.5*self.args.thickness * self.hall_width

    def random_map(self):
        self.set_maze_grid()
        self.set_walls()
        self.map_for_LM = fill_outer_rim(self.map_for_LM, self.map_rows, self.map_cols)
        if self.args.distort_map:
            self.map_for_LM = distort_map(self.map_for_LM, self.map_rows, self.map_cols)
            self.map_for_LM = fill_outer_rim(self.map_for_LM, self.map_rows, self.map_cols)
            

    def random_box(self):
        #rooms_row: number of rooms in a row [a,b): a <= n < b
        #rooms_col: number of rooms in a col [a,b): a <= n < b

        kwargs = {'rooms_row':(2,3), 'rooms_col':(1,3),
                  'slant_scale':2, 'n_boxes':(1,8), 'thick':50, 'thick_scale':3}
        ps = PartitionSpace(**kwargs)
        # p_open : probability to have the doors open between rooms
        ps.connect_rooms(p_open=1.0)

        # set output map size
        self.map_for_LM = ps.get_map(self.map_rows,self.map_cols)
        
        
        
    def read_map(self):
        ''' 
        set map_design (grid_rows x grid_cols), 
        map_2d (map_rows x map_cols), 
        map_for_RL for RL state (n_state_grids x n_state_grids)
        '''

        self.map_for_LM = np.load(self.args.load_map)
        # self.map_for_pose = np.load(self.args.load_map_LM)
        # mdt = np.load(self.args.load_map_RL)
        # self.map_for_RL[0,:,:] = torch.tensor(mdt).float().to(self.device)
            
    def set_walls(self):
        ''' 
        set map_design, map_2d, map_for_RL
        '''
        if self.args.test_mode:
            map_file = os.path.join(self.args.test_data_path, "map-design-%05d.npy"%self.env_count)
            maze = np.load(map_file)

        else:            
            if self.args.random_rm_cells[1]>0:
                low=self.args.random_rm_cells[0]
                high=self.args.random_rm_cells[1]
                num_cells_to_delete = np.random.randint(low, high)
            else:
                num_cells_to_delete = self.args.rm_cells

            if self.args.save_boundary == 'y':
                save_boundary = True
            elif self.args.save_boundary == 'n':
                save_boundary = False
            else:
                save_boundary = True if np.random.random()>0.5 else False
            maze_options = {'save_boundary': save_boundary,
                            "min_blocks": 10}
            maze = generate_map(self.n_maze_grids, num_cells_to_delete, **maze_options )

        for i in range(self.n_maze_grids):
            for j in range(self.n_maze_grids):
                if i < self.n_maze_grids-1:
                    if maze[i,j]==1 and maze[i+1,j]==1:
                        #place vertical
                        self.set_a_wall([i,j],[i+1,j],self.n_maze_grids,horizontal=False)
                if j < self.n_maze_grids-1:
                    if maze[i,j]==1 and maze[i,j+1] ==1:
                        #place horizontal wall
                        self.set_a_wall([i,j],[i,j+1],self.n_maze_grids,horizontal=True)
                if i>0 and i<self.n_maze_grids-1 and j>0 and j<self.n_maze_grids-1:
                    if maze[i,j]==1 and maze[i-1,j] == 0 and maze[i+1,j]==0 and maze[i,j-1]==0 and maze[i,j+1]==0:
                        self.set_a_pillar([i,j], self.n_maze_grids)


    def make_low_dim_maps(self):
        self.map_for_pose = cv2.resize(self.map_for_LM, (self.grid_rows, self.grid_cols),interpolation=cv2.INTER_AREA)
        self.map_for_pose = normalize(self.map_for_pose)
        self.map_for_pose = np.clip(self.map_for_pose, 0.0, 1.0)

        mdt = cv2.resize(self.map_for_LM,(self.args.n_state_grids,self.args.n_state_grids), interpolation=cv2.INTER_AREA)
        mdt = normalize(mdt)
        mdt = np.clip(mdt, 0.0, 1.0)
        self.map_for_RL[0,:,:] = torch.tensor(mdt).float().to(self.device)


    def clear_objects(self):
        self.map_for_LM = np.zeros((self.map_rows, self.map_cols))
        self.map_for_pose = np.zeros((self.grid_rows, self.grid_cols),dtype='float')
        self.map_for_RL = torch.zeros((1,self.args.n_state_grids, self.args.n_state_grids),device=torch.device(self.device))
        
        
                        
    def set_a_pillar(self, a, grids):
        x=to_real(a[0], self.xlim, grids)
        y=to_real(a[1], self.ylim, grids)

        #rad = self.obs_radius
        if self.args.backward_compatible_maps:
            rad = 0.15
        elif self.args.random_thickness:
            rad = np.random.normal(loc=self.obs_radius, scale=self.hall_width*0.25)
            rad = np.clip(rad, self.hall_width*0.25, self.hall_width*0.5)
        else:
            rad = self.obs_radius


        corner0 = [x+rad,y+rad]
        corner1 = [x-rad,y-rad]
        x0 = to_index(corner0[0], self.map_rows, self.xlim)
        y0 = to_index(corner0[1], self.map_cols, self.ylim)
        x1 = to_index(corner1[0], self.map_rows, self.xlim)
        y1 = to_index(corner1[1], self.map_cols, self.ylim)
        for ir in range(x0,x1+1):
            for ic in range(y0,y1+1):
                dx = to_real(ir, self.xlim, self.map_rows) - x
                dy = to_real(ic, self.ylim, self.map_cols) - y
                dist = np.sqrt(dx**2+dy**2)
                if dist <= rad:
                    self.map_for_LM[ir,ic]=1.0

                        
    def set_a_wall(self,a,b,grids,horizontal=True):
        ax = to_real(a[0], self.xlim, grids)
        ay = to_real(a[1], self.ylim, grids)
        bx = to_real(b[0], self.xlim, grids)
        by = to_real(b[1], self.ylim, grids)

        # if horizontal:
        #     yaw=math.radians(90)
        # else:
        #     yaw=math.radians(0)

        #rad = self.obs_radius
        if self.args.backward_compatible_maps:
            rad = 0.1*np.ones(4)
        elif self.args.random_thickness:
            rad = np.random.normal(loc=self.obs_radius, scale=self.hall_width*0.25, size=4)
            rad = np.clip(rad, self.hall_width*0.1, self.hall_width*0.5)
        else:
            rad = self.obs_radius*np.ones(4)

        corner0 = [ax+rad[0],ay+rad[1]]
        corner1 = [bx-rad[2],by-rad[3]]

        x0 = to_index(corner0[0], self.map_rows, self.xlim)
        y0 = to_index(corner0[1], self.map_cols, self.ylim)

        if self.args.backward_compatible_maps:
            x1 = to_index(corner1[0], self.map_rows, self.xlim)
            y1 = to_index(corner1[1], self.map_cols, self.ylim)
        else:
            x1 = to_index(corner1[0], self.map_rows, self.xlim)#+1
            y1 = to_index(corner1[1], self.map_cols, self.ylim)#+1

        self.map_for_LM[x0:x1, y0:y1]=1.0

        # x0 = to_index(corner0[0], self.grid_rows, self.xlim)
        # y0 = to_index(corner0[1], self.grid_cols, self.ylim)
        # x1 = to_index(corner1[0], self.grid_rows, self.xlim)+1
        # y1 = to_index(corner1[1], self.grid_cols, self.ylim)+1

        # self.map_for_pose[x0:x1, y0:y1]=1.0
    def sample_a_pose(self):
        # new turtle location (random)
        check = True
        collision_radius = 0.50
        while (check):
            turtle_can = range(self.grid_rows*self.grid_cols)
            turtle_bin = np.random.choice(turtle_can,1)

            self.true_grid.row = turtle_bin//self.grid_cols
            self.true_grid.col = turtle_bin% self.grid_cols
            self.true_grid.head = np.random.randint(self.grid_dirs)
            self.goal_pose.x = to_real(self.true_grid.row, self.xlim, self.grid_rows)
            self.goal_pose.y = to_real(self.true_grid.col, self.ylim, self.grid_cols)
            self.goal_pose.theta = wrap(self.true_grid.head*self.heading_resol)
            check =  self.collision_fnc(self.goal_pose.x, self.goal_pose.y, collision_radius, self.map_for_LM)

    def set_init_pose(self):

        self.true_grid.head = self.args.init_pose[0]
        self.true_grid.row = self.args.init_pose[1]
        self.true_grid.col = self.args.init_pose[2]
        self.goal_pose.x = to_real(self.true_grid.row, self.xlim, self.grid_rows)
        self.goal_pose.y = to_real(self.true_grid.col, self.ylim, self.grid_cols)
        self.goal_pose.theta = wrap(self.true_grid.head*self.heading_resol)
        check = True
        cnt = 0
        while (check):
            if cnt > 100:
                return False
            cnt += 1
            if self.args.init_error == "XY" or self.args.init_error == "BOTH":
                delta_x = (0.5-np.random.rand())*(self.xlim[1]-self.xlim[0])/self.grid_rows
                delta_y = (0.5-np.random.rand())*(self.ylim[1]-self.ylim[0])/self.grid_cols
            else:
                delta_x=0
                delta_y=0
            if self.args.init_error == "THETA" or self.args.init_error == "BOTH":
                delta_theta =  (0.5-np.random.rand())*self.heading_resol
            else:
                delta_theta=0
            self.perturbed_goal_pose.x = self.goal_pose.x+delta_x
            self.perturbed_goal_pose.y = self.goal_pose.y+delta_y
            self.perturbed_goal_pose.theta = self.goal_pose.theta+delta_theta

            check =  self.collision_fnc(self.perturbed_goal_pose.x, self.perturbed_goal_pose.y, self.collision_radius, self.map_for_LM)
        self.teleport_turtle()
        self.update_true_grid()
        return True
    
    def place_turtle(self):
        # new turtle location (random)
        check = True
        cnt = 0
        while (check):
            if cnt > 100:
                return False
            cnt += 1            
            turtle_can = range(self.grid_rows*self.grid_cols)
            turtle_bin = np.random.choice(turtle_can,1)

            self.true_grid.row = turtle_bin//self.grid_cols
            self.true_grid.col = turtle_bin% self.grid_cols
            self.true_grid.head = np.random.randint(self.grid_dirs)
            self.goal_pose.x = to_real(self.true_grid.row, self.xlim, self.grid_rows)
            self.goal_pose.y = to_real(self.true_grid.col, self.ylim, self.grid_cols)
            self.goal_pose.theta = wrap(self.true_grid.head*self.heading_resol)
            check =  self.collision_fnc(self.goal_pose.x, self.goal_pose.y, self.collision_radius, self.map_for_LM)

        check = True
        cnt = 0
        while (check):
            if cnt > 100:
                return False
            cnt += 1
            if self.args.init_error == "XY" or self.args.init_error == "BOTH":
                delta_x = (0.5-np.random.rand())*(self.xlim[1]-self.xlim[0])/self.grid_rows
                delta_y = (0.5-np.random.rand())*(self.ylim[1]-self.ylim[0])/self.grid_cols
            else:
                delta_x=0
                delta_y=0
            if self.args.init_error == "THETA" or self.args.init_error == "BOTH":
                delta_theta =  (0.5-np.random.rand())*self.heading_resol
            else:
                delta_theta=0
            self.perturbed_goal_pose.x = self.goal_pose.x+delta_x
            self.perturbed_goal_pose.y = self.goal_pose.y+delta_y
            self.perturbed_goal_pose.theta = self.goal_pose.theta+delta_theta

            check =  self.collision_fnc(self.perturbed_goal_pose.x, self.perturbed_goal_pose.y, self.collision_radius, self.map_for_LM)


        if self.args.test_mode:
            pg_pose_file = os.path.join(self.args.test_data_path, "pg-pose-%05d.npy"%self.env_count)
            g_pose_file = os.path.join(self.args.test_data_path, "g-pose-%05d.npy"%self.env_count)
            pg_pose = np.load(pg_pose_file)
            g_pose = np.load(g_pose_file)
            self.goal_pose.theta = g_pose[0]
            self.goal_pose.x = g_pose[1]
            self.goal_pose.y = g_pose[2]
            if self.args.init_error == "XY" or self.args.init_error == "BOTH":
                self.perturbed_goal_pose.x = pg_pose[1]
                self.perturbed_goal_pose.y = pg_pose[2]
            else:
                self.perturbed_goal_pose.x = g_pose[1]
                self.perturbed_goal_pose.y = g_pose[2]
            if self.args.init_error == "THETA" or self.args.init_error == "BOTH":
                self.perturbed_goal_pose.theta = pg_pose[0]
            else:
                self.perturbed_goal_pose.theta = g_pose[0]

        if self.args.verbose > 1:
            print ('gt_row,col,head = %f,%f,%d'%(self.true_grid.row,self.true_grid.col,self.true_grid.head))
            print('x_goal,y_goal,target_ori=%f,%f,%f'%(self.goal_pose.x,self.goal_pose.y,self.goal_pose.theta))
        # self.turtle_pose_msg.position.x = self.goal_pose.x
        # self.turtle_pose_msg.position.y = self.goal_pose.y
        # yaw = self.goal_pose.theta
        
        # self.turtle_pose_msg.orientation = geometry_msgs.msg.Quaternion(*tf_conversions.transformations.quaternion_from_euler(0, 0, yaw))
        self.teleport_turtle()
        self.update_true_grid()
        # self.update_current_pose()
        return True
    
        
    def reset_explored(self): # reset explored area to all 0's
        self.explored_space = np.zeros((self.grid_dirs,self.grid_rows, self.grid_cols),dtype='float')
        self.new_pose = False
        return

    def update_bel_list(self):
        guess = self.bel_grid
        # guess = np.unravel_index(np.argmax(self.belief.cpu().detach().numpy(), axis=None), self.belief.shape)
        if guess not in self.bel_list:
            self.new_bel = True
            self.bel_list.append(guess)
            if self.args.verbose > 2:
                print ("bel_list", len(self.bel_list))
        else:
            self.new_bel = False

    def update_explored(self):
        if self.explored_space[self.true_grid.head,self.true_grid.row, self.true_grid.col] == 0.0:
            self.new_pose = True
        else:
            self.new_pose = False
        self.explored_space[self.true_grid.head,self.true_grid.row, self.true_grid.col] = 1.0
        return

    def normalize_gtl(self):
        gt = self.gt_likelihood
        self.gt_likelihood_unnormalized = np.copy(self.gt_likelihood)
        if self.args.gtl_output == "softmax":
            gt = softmax(gt, self.args.temperature)
            # gt = torch.from_numpy(softmax(gt)).float().to(self.device)
        elif self.args.gtl_output == "softermax":
            gt = softermax(gt)
            # gt = torch.from_numpy(softmin(gt)).float().to(self.device)
        elif self.args.gtl_output == "linear":
            gt = np.clip(gt, 1e-5, 1.0)
            gt=gt/gt.sum()
            # gt = torch.from_numpy(gt/gt.sum()).float().to(self.device)
        # self.gt_likelihood = torch.tensor(gt).float().to(self.device)
        self.gt_likelihood = gt


    def get_gtl_cos_mp(self, ref_scans, scan_data, my_dirs, return_dict):
        chk_rad = 0.05
        offset = 360.0/self.grid_dirs
        y= np.array(scan_data.ranges_2pi)[::self.args.pm_scan_step]
        y = np.clip(y, self.min_scan_range, self.max_scan_range)
        # y = np.clip(y, self.min_scan_range, np.inf)
        for heading in my_dirs:
            X = np.roll(ref_scans, -int(offset*heading),axis=2)[:,:,::self.args.pm_scan_step]
            gtl = np.zeros((self.grid_rows, self.grid_cols))
            for i_ld in range(self.grid_rows):
                for j_ld in range(self.grid_cols):
                    if self.collision_fnc(to_real(i_ld, self.xlim, self.grid_rows), to_real(j_ld, self.ylim, self.grid_cols), chk_rad, self.map_for_LM):
                    # if self.map_for_pose[i_ld, j_ld]>0.4:
                        gtl[i_ld,j_ld]=0.0
                    else:
                        x = X[i_ld,j_ld,:]
                        x = np.clip(x, self.min_scan_range, self.max_scan_range)
                        # x = np.clip(x, self.min_scan_range, np.inf)                        
                        gtl[i_ld,j_ld] = self.get_cosine_sim(x,y)
            ###
            return_dict[heading] = {'gtl': gtl}


    def get_gtl_cos_mp2(self, my_dirs, scan_data, return_dict):
        chk_rad = 0.05
        offset = 360.0/self.grid_dirs
        y= np.array(scan_data.ranges_2pi)[::self.args.pm_scan_step]
        y = np.clip(y, self.min_scan_range, self.max_scan_range)
        for heading in my_dirs:
            X = np.roll(self.scans_over_map, -int(offset*heading), axis=2)[:,:,::self.args.pm_scan_step]
            gtl = np.zeros((self.grid_rows, self.grid_cols))
            for i_ld in range(self.grid_rows):
                for j_ld in range(self.grid_cols):
                    if self.collision_fnc(to_real(i_ld, self.xlim, self.grid_rows), to_real(j_ld, self.ylim, self.grid_cols), chk_rad, self.map_for_LM):
                    # if self.map_for_pose[i_ld, j_ld]>0.4:
                        gtl[i_ld,j_ld]=0.0
                    else:
                        x = X[i_ld,j_ld,:]
                        x = np.clip(x, self.min_scan_range, self.max_scan_range)
                        gtl[i_ld,j_ld] = self.get_cosine_sim(x,y)
            ###
            return_dict[heading] = {'gtl': gtl}

            
    def get_gtl_corr_mp(self, ref_scans, my_dirs, return_dict, clip):
        chk_rad = 0.05
        offset = 360/self.grid_dirs
        y= np.array(self.scan_data_at_unperturbed.ranges_2pi)[::self.args.pm_scan_step]
        y = np.clip(y, self.min_scan_range, self.max_scan_range)
        for heading in my_dirs:
            X = np.roll(ref_scans, -offset*heading,axis=2)[:,:,::self.args.pm_scan_step]
            gtl = np.zeros((self.grid_rows, self.grid_cols))
            for i_ld in range(self.grid_rows):
                for j_ld in range(self.grid_cols):
                    if self.collision_fnc(to_real(i_ld, self.xlim, self.grid_rows), to_real(j_ld, self.ylim, self.grid_cols), chk_rad, self.map_for_LM):
                    # if self.map_for_pose[i_ld, j_ld]>0.4:
                        gtl[i_ld,j_ld]=0.0
                    else:
                        x = X[i_ld,j_ld,:]
                        x = np.clip(x, self.min_scan_range, self.max_scan_range)
                        gtl[i_ld,j_ld] = self.get_corr(x,y,clip=clip)
            ###
            return_dict[heading] = {'gtl': gtl}


    def get_gt_likelihood_cossim(self, ref_scans, scan_data):
        # start_time = time.time()
        manager = multiprocessing.Manager()
        return_dict = manager.dict()

        accum = 0
        procs = []
        for i_worker in range(min(self.args.n_workers, self.grid_dirs)):
            n_dirs = self.grid_dirs//self.args.n_workers
            if i_worker < self.grid_dirs % self.args.n_workers:
                n_dirs +=1
            my_dirs = range(accum, accum+n_dirs)
            accum += n_dirs
            if len(my_dirs)>0:
                pro = multiprocessing.Process(target = self.get_gtl_cos_mp,
                                          args = [ref_scans, scan_data, my_dirs, return_dict])
                procs.append(pro)

        [pro.start() for pro in procs]
        [pro.join() for pro in procs]

        gtl = np.ones((self.grid_dirs,self.grid_rows,self.grid_cols))
        for i in range(self.grid_dirs):
            ret = return_dict[i]    
            gtl[i,:,:] = ret['gtl']
        return gtl

        # for i in range(self.grid_dirs):
        #     ret = return_dict[i]    
        #     self.gt_likelihood[i,:,:] = ret['gtl']
        #     # self.gt_likelihood[i,:,:] = torch.tensor(ret['gtl']).float().to(self.device)

            
    def get_gt_likelihood_cossim2(self, scan_data):
        # start_time = time.time()
        manager = multiprocessing.Manager()
        return_dict = manager.dict()

        accum = 0
        procs = []
        for i_worker in range(min(self.args.n_workers, self.grid_dirs)):
            n_dirs = self.grid_dirs//self.args.n_workers
            if i_worker < self.grid_dirs % self.args.n_workers:
                n_dirs +=1
            my_dirs = range(accum, accum+n_dirs)
            accum += n_dirs
            if len(my_dirs)>0:
                pro = multiprocessing.Process(target = self.get_gtl_cos_mp2,
                                          args = [ref_scans, scan_data, my_dirs, return_dict])
                procs.append(pro)

        [pro.start() for pro in procs]
        [pro.join() for pro in procs]

        gtl = np.ones((self.grid_dirs,self.grid_rows,self.grid_cols))

        for i in range(self.grid_dirs):
            ret = return_dict[i]    
            gtl[i,:,:] = ret['gtl']

        return gtl

    def get_gt_likelihood_corr(self, ref_scans, clip=0):
        # start_time = time.time()
        manager = multiprocessing.Manager()
        return_dict = manager.dict()

        accum = 0
        procs = []
        for i_worker in range(min(self.args.n_workers, self.grid_dirs)):
            n_dirs = self.grid_dirs//self.args.n_workers
            if i_worker < self.grid_dirs % self.args.n_workers:
                n_dirs +=1
            my_dirs = range(accum, accum+n_dirs)
            accum += n_dirs
            if len(my_dirs)>0:
                pro = multiprocessing.Process(target = self.get_gtl_corr_mp,
                                              args = [ref_scans, my_dirs, return_dict, clip])
                procs.append(pro)

        [pro.start() for pro in procs]
        [pro.join() for pro in procs]

        for i in range(self.grid_dirs):
            ret = return_dict[i]    
            self.gt_likelihood[i,:,:] = ret['gtl']
            # self.gt_likelihood[i,:,:] = torch.tensor(ret['gtl']).float().to(self.device)
        

    def get_cosine_sim(self,x,y):
        # numpy arrays.
        non_inf_x = ~np.isinf(x)
        non_nan_x = ~np.isnan(x)
        non_inf_y = ~np.isinf(y)
        non_nan_y = ~np.isnan(y)

        numbers_only = non_inf_x & non_nan_x & non_inf_y & non_nan_y
        x=x[numbers_only]
        y=y[numbers_only]
        return sum(x*y)/np.linalg.norm(y,2)/np.linalg.norm(x,2)


    def get_corr(self,x,y,clip=1):
        mx=np.mean(x)
        my=np.mean(y)
        corr=sum((x-mx)*(y-my))/np.linalg.norm(y-my,2)/np.linalg.norm(x-mx,2)
        # return 0.5*(corr+1.0)
        if clip==1:
            return np.clip(corr, 0, 1.0)
        else:
            return 0.5*(corr+1.0)
        
    def get_a_scan(self, x_real, y_real, offset=0, scan_step=1, noise=0, sigma=0, fov=False):
        #class member variables: map_rows, map_cols, xlim, ylim, min_scan_range, max_scan_range, map_2d
        
        row_hd = to_index(x_real, self.map_rows, self.xlim)  # from real to hd
        col_hd = to_index(y_real, self.map_cols, self.ylim)  # from real to hd
        scan = np.zeros(360)
        missing = np.random.choice(360, noise, replace=False)
        gaussian_noise = np.random.normal(scale=sigma, size=360)
        for i_ray in range(0,360, scan_step):
            if fov and i_ray > self.args.fov[0] and i_ray < self.args.fov[1]:
                scan[i_ray]=np.nan
                continue
            else:
                pass
            
            theta = math.radians(i_ray)+offset
            if i_ray in missing:
                dist = np.inf
            else:
                dist = self.min_scan_range
            while True:
                if dist >= self.max_scan_range: 
                    dist = np.inf
                    break
                x_probe = x_real + dist * np.cos(theta)
                y_probe = y_real + dist * np.sin(theta)
                # see if there's something
                i_hd_prb = to_index(x_probe, self.map_rows, self.xlim)
                j_hd_prb = to_index(y_probe, self.map_cols, self.ylim)
                if i_hd_prb < 0 or i_hd_prb >= self.map_rows: 
                    dist = np.inf
                    break
                if j_hd_prb < 0 or j_hd_prb >= self.map_cols: 
                    dist = np.inf
                    break
                if self.map_for_LM[i_hd_prb, j_hd_prb] >= 0.5: 
                    break
                dist += 0.01+0.01*(np.random.rand())
            scan[i_ray]=dist+gaussian_noise[i_ray]
        return scan
        

    def get_a_scan_mp(self, range_place, return_dict, offset=0, scan_step=1, map_img=None, xlim=None, ylim=None, fov=False):

        # print (os.getpid(), min(range_place), max(range_place))
        for i_place in range_place:
        #class member variables: map_rows, map_cols, xlim, ylim, min_scan_range, max_scan_range, map_2d
            row_ld = i_place // self.grid_cols
            col_ld = i_place %  self.grid_cols
            x_real = to_real(row_ld, xlim, self.grid_rows ) # from low-dim location to real
            y_real = to_real(col_ld, ylim, self.grid_cols ) # from low-dim location to real
            row_hd = to_index(x_real, self.map_rows, xlim)  # from real to hd
            col_hd = to_index(y_real, self.map_cols, ylim)  # from real to hd
            scan = np.zeros(360)
        
            for i_ray in range(0,360, scan_step):
                if fov and i_ray > self.args.fov[0] and i_ray < self.args.fov[1]:
                    scan[i_ray]=np.nan
                    continue
                else:
                    pass
                
                theta = math.radians(i_ray)+offset
                dist = self.min_scan_range
                while True:
                    if dist >= self.max_scan_range: 
                        dist = np.inf
                        break
                    x_probe = x_real + dist * np.cos(theta)
                    y_probe = y_real + dist * np.sin(theta)
                    # see if there's something
                    i_hd_prb = to_index(x_probe, self.map_rows, xlim)
                    j_hd_prb = to_index(y_probe, self.map_cols, ylim)
                    if i_hd_prb < 0 or i_hd_prb >= self.map_rows: 
                        dist = np.inf
                        break
                    if j_hd_prb < 0 or j_hd_prb >= self.map_cols: 
                        dist = np.inf
                        break
                    if map_img[i_hd_prb, j_hd_prb] >= 0.5: 
                        break
                    dist += 0.01+0.01*(np.random.rand())
                scan[i_ray]=dist
            #return scan
            return_dict[i_place]={'scan':scan}

        
    # def get_synth_scan(self):
    #     # start_time = time.time()                
    #     # place sensor at a location, then reach out in 360 rays all around it and record when each ray gets hit.
    #     n_places=self.grid_rows * self.grid_cols

    #     for i_place in range(n_places):
    #         row_ld = i_place // self.grid_cols
    #         col_ld = i_place %  self.grid_cols
    #         x_real = to_real(row_ld, self.xlim, self.grid_rows ) # from low-dim location to real
    #         y_real = to_real(col_ld, self.ylim, self.grid_cols ) # from low-dim location to real
    #         scan = self.get_a_scan(x_real, y_real,scan_step=self.args.pm_scan_step)
    #         self.scans_over_map[row_ld, col_ld,:] = np.clip(scan, 1e-10, self.max_scan_range)
    #         if i_place%10==0: print ('.')

    #     # print ('scans', time.time()-start_time)
        

    
    def get_synth_scan_mp(self, scans, map_img=None, xlim=None, ylim=None):

        # print (multiprocessing.cpu_count())
        # start_time = time.time()    
        # place sensor at a location, then reach out in 360 rays all around it and record when each ray gets hit.
        n_places=self.grid_rows * self.grid_cols
        
        manager = multiprocessing.Manager()
        return_dict = manager.dict()
        procs = []
        
        accum = 0
        for worker in range(min(self.args.n_workers, n_places)):
            n_myplaces = n_places//self.args.n_workers
            if worker < n_places % self.args.n_workers:
                n_myplaces += 1
            range_place = range(accum, accum+n_myplaces)
            accum += n_myplaces

            kwargs = {'scan_step': self.args.pm_scan_step, 'map_img':map_img, 'xlim':xlim, 'ylim':ylim, 'fov':False}
            pro = multiprocessing.Process(target = self.get_a_scan_mp, args = [range_place, return_dict ], kwargs = kwargs)
            procs.append(pro)

        [pro.start() for pro in procs]
        [pro.join() for pro in procs]
        
        # scans = np.ndarray((self.grid_rows*self.grid_cols, 360))

        for i_place in range(n_places):
            ### multi-processing
            rd = return_dict[i_place]
            scan = rd['scan']
            # scans [i_place, :] = np.clip(scan, self.min_scan_range, self.max_scan_range)
            row_ld = i_place // self.grid_cols
            col_ld = i_place %  self.grid_cols
            # scans[row_ld, col_ld,:] = np.clip(scan, self.min_scan_range, np.inf)            
            scans[row_ld, col_ld,:] = np.clip(scan, self.min_scan_range, self.max_scan_range)
            # self.scans_over_map[row_ld, col_ld,:] = np.clip(scan, self.min_scan_range, self.max_scan_range)

        
    def slide_scan(self):
        # slide scan_2d downward for self.front_margin_pixels, and then left/righ for collision radius
        self.scan_2d_slide = np.copy(self.scan_2d[0,:,:])
        for i in range(self.front_margin_pixels):
            self.scan_2d_slide += shift(self.scan_2d_slide, 1, axis=0, fill=1.0)
        # self.scan_2d_slide = np.clip(self.scan_2d_slide,0.0,1.0)
        for i in range(self.side_margin_pixels):
            self.scan_2d_slide += shift(self.scan_2d_slide, +1, axis=1, fill=1.0)
            self.scan_2d_slide += shift(self.scan_2d_slide, -1, axis=1, fill=1.0)
        self.scan_2d_slide = np.clip(self.scan_2d_slide,0.0,1.0)


    def get_scan_2d_n_headings(self, scan_data, xlim, ylim):
        if self.args.verbose > 1:
            print('get_scan_2d_n_headings')

        data = scan_data
        if self.map_rows == None :
            return None, None
        if self.map_cols == None:
            return None, None

        O=self.grid_dirs
        N=self.map_rows
        M=self.map_cols
        scan_2d = np.zeros(shape=(O,N,M))
        angles = np.linspace(data.angle_min, data.angle_max, data.ranges.size, endpoint=False)

        for i,dist in enumerate(data.ranges):
            for rotate in range(O):
                offset = 2*np.pi/O*rotate
                angle = offset + angles[i]
                if angle > math.radians(self.args.fov[0]) and angle < math.radians(self.args.fov[1]):
                    continue
                if ~np.isinf(dist) and ~np.isnan(dist):
                    x = (dist)*np.cos(angle)
                    y = (dist)*np.sin(angle)
                    n = to_index(x, N, xlim)
                    m = to_index(y, M, ylim)
                    if n>=0 and n<N and m>0 and m<M:
                        scan_2d[rotate,n,m] = 1.0

        rows1 = self.args.n_state_grids
        cols1 = self.args.n_state_grids
        rows2 = self.args.n_local_grids
        cols2 = rows2

        center=self.args.n_local_grids//2

        if self.args.binary_scan:
            scan_2d_low = np.ceil(normalize(cv2.resize(scan_2d[0,:,:], (rows1, cols1),interpolation=cv2.INTER_AREA)))
        else:
            scan_2d_low = normalize(cv2.resize(scan_2d[0,:,:], (rows1, cols1),interpolation=cv2.INTER_AREA))

        return scan_2d, scan_2d_low             

    
    def do_scan_2d_n_headings(self):
        if self.args.verbose > 1:
            print('get_scan_2d_n_headings')

        data = self.scan_data
        if self.map_rows == None :
            return
        if self.map_cols == None:
            return

        O=self.grid_dirs
        N=self.map_rows
        M=self.map_cols
        self.scan_2d = np.zeros(shape=(O,N,M))
        angles = np.linspace(data.angle_min, data.angle_max, data.ranges.size, endpoint=False)

        for i,dist in enumerate(data.ranges):
            for rotate in range(O):
                offset = 2*np.pi/O*rotate
                angle = offset + angles[i]
                if angle > math.radians(self.args.fov[0]) and angle < math.radians(self.args.fov[1]):
                    continue
                if ~np.isinf(dist) and ~np.isnan(dist):
                    x = (dist)*np.cos(angle)
                    y = (dist)*np.sin(angle)
                    n = to_index(x, N, self.xlim)
                    m = to_index(y, M, self.ylim)
                    if n>=0 and n<N and m>0 and m<M:
                        self.scan_2d[rotate,n,m] = 1.0

        rows1 = self.args.n_state_grids
        cols1 = self.args.n_state_grids
        rows2 = self.args.n_local_grids
        cols2 = rows2

        center=self.args.n_local_grids//2

        if self.args.binary_scan:
            self.scan_2d_low = np.ceil(normalize(cv2.resize(self.scan_2d[0,:,:], (rows1, cols1),interpolation=cv2.INTER_AREA)))
        else:
            self.scan_2d_low = normalize(cv2.resize(self.scan_2d[0,:,:], (rows1, cols1),interpolation=cv2.INTER_AREA))
        return


    def generate_data(self):
        # data index: D
        # n envs : E
        # n episodes: N
        # file-number(D) = D//N = E, 
        # data index in the file = D % N
        # map file number = D//N = E

        index = "%05d"%(self.data_cnt)
        target_data = self.gt_likelihood_unnormalized
        range_data=np.array(self.scan_data.ranges)
        angle_array = np.linspace(self.scan_data.angle_min, self.scan_data.angle_max,range_data.size, endpoint=False)
        scan_data_to_save = np.stack((range_data,angle_array),axis=1) #first column: range, second column: angle

        self.target_list.append(target_data)
        self.scan_list.append(scan_data_to_save)
        if self.args.verbose > 2:
            print ("target_list", len(self.target_list))
            print ("scan_list", len(self.scan_list))

        if self.done:
            scans = np.stack(self.scan_list, axis=0)
            targets = np.stack(self.target_list, axis=0)
            np.save(os.path.join(self.data_path, 'scan-%s.npy'%index), scans)
            np.save(os.path.join(self.data_path, 'map-%s.npy'%index), self.map_for_LM)
            np.save(os.path.join(self.data_path, 'target-%s.npy'%index), targets)
            self.scan_list = []
            self.target_list = []
            self.data_cnt+=1
            if args.verbose > 0:
                print ("%d: map %s, scans %s, targets %s"%(index, self.map_for_LM.shape, scans.shape, targets.shape ))
        return


    def stack_data(self):

        target_data = self.gt_likelihood_unnormalized
        range_data = np.array(self.scan_data.ranges_2pi, np.float32)
        angle_array = np.array(self.scan_data.angles_2pi, np.float32)
        scan_data_to_save = np.stack((range_data,angle_array),axis=1) #first column: range, second column: angle

        self.target_list.append(target_data)
        self.scan_list.append(scan_data_to_save)
        if self.args.verbose > 2:
            print ("target_list", len(self.target_list))
            print ("scan_list", len(self.scan_list))


    def save_generated_data(self):
        scans = np.stack(self.scan_list, axis=0)
        targets = np.stack(self.target_list, axis=0)
        np.save(os.path.join(self.data_path, 'scan-%05d.npy'%self.data_cnt), scans)
        np.save(os.path.join(self.data_path, 'map-%05d.npy'%self.data_cnt), self.map_for_LM)
        np.save(os.path.join(self.data_path, 'target-%05d.npy'%self.data_cnt), targets)
        if args.verbose > 0:
            print ("%05d: map %s, scans %s, targets %s"%(self.data_cnt, self.map_for_LM.shape, scans.shape, targets.shape ))
        self.scan_list = []
        self.target_list = []
        self.data_cnt+=1


    def collect_data(self):
        # ENV-EPI-STP-CNT
        # map, scan, belief, likelihood, GTL, policy, action, reward
        # input = [map, scan]
        # target = [GTL]
        # state = [map-low-dim, bel, scan-low-dim]
        # action_reward = [action, p0, p1, p2, reward]

        # index = "%03d-%03d-%03d-%04d"%(self.env_count,self.episode_count,self.step_count,self.data_cnt)
        index = "%05d"%(self.data_cnt)
        env_index = "%05d"%(self.env_count)

        with open(self.rollout_list,'a') as ro:
            ro.write('%d %d %d %d\n'%(self.env_count,self.episode_count,self.step_count,self.data_cnt))

        map_file = os.path.join(self.data_path, 'map-%s.npy'%env_index)
        if not os.path.isfile(map_file):
            #save the map
            np.save(map_file, self.map_for_LM)

        target_data = self.gt_likelihood_unnormalized
        gt_pose = np.array((self.true_grid.head,self.true_grid.row,self.true_grid.col)).reshape(1,-1)
        map_num = np.array([self.env_count])
        range_data=np.array(self.scan_data.ranges)
        angle_array = np.linspace(self.scan_data.angle_min, self.scan_data.angle_max,range_data.size, endpoint=False)
        scan_data_to_save = np.stack((range_data,angle_array),axis=1) #first column: range, second column: angle

        real_pose = np.array((self.current_pose.theta, self.current_pose.x, self.current_pose.y)).reshape(1,-1)

        dict_to_save = {'scan':scan_data_to_save, 
                        'mapindex':map_num, 
                        'target':target_data, 
                        'belief': self.belief.detach().cpu().numpy(), 
                        'like':self.likelihood.detach().cpu().numpy(), 
                        'action': self.action_idx,
                        'prob':self.prob.reshape(1,-1),
                        'reward': self.reward_vector.reshape(1,-1),
                        'gt_pose': gt_pose,
                        'real_pose': real_pose}

        np.save(os.path.join(self.data_path, 'data-%s.npy'%index), dict_to_save)

        self.data_cnt+=1
        return


    def compute_gtl(self, ref_scans):
        if self.args.gtl_off == True:
            gt = np.random.rand(self.grid_dirs, self.grid_rows, self.grid_cols)
            gt = np.clip(gt, 1e-5, 1.0)
            gt=gt/gt.sum()
            self.gt_likelihood = gt
            # self.gt_likelihood = torch.tensor(gt).float().to(self.device)
        else:
            if self.args.gtl_src == 'hd-corr':
                self.get_gt_likelihood_corr(ref_scans, clip=0)
            elif self.args.gtl_src == 'hd-corr-clip':
                self.get_gt_likelihood_corr(ref_scans, clip=1)
            elif self.args.gtl_src == 'hd-cos':
                self.gt_likelihood = self.get_gt_likelihood_cossim(ref_scans, self.scan_data_at_unperturbed)
            else:
                raise Exception('GTL source required: --gtl-src= [low-dim-map, high-dim-map]')
            self.normalize_gtl()

    def run_action_module(self, no_update_fig=False):
        if self.args.random_policy:
            fwd_collision = self.collision_fnc(0, 0, 0, self.scan_2d_slide)
            if fwd_collision:
                num_actions = 2
            else:
                num_actions = 3
            self.action_from_policy = np.random.randint(num_actions)
            self.action_str = self.action_space[self.action_from_policy]
        elif self.args.navigate_to is not None:
            self.navigate()
        else:
            mark_time = time.time()
            self.get_action()
            self.action_time = time.time()-mark_time
            print('[ACTION] %.3f sec '%(time.time()-mark_time))

        if no_update_fig:
            return
            
        if self.args.figure:
            # update part of figure after getting action
            self.ax_map.set_title('action(%d):%s'%(self.step_count,self.action_str))
            ax = self.ax_act
            self.update_act_dist(ax)
            ax=self.ax_rew
            act_lttr=['L','R','F','-']
            self.obj_rew= self.update_list(ax,self.rewards,self.obj_rew,"Reward", text=act_lttr[self.action_idx])
            ax=self.ax_err
            self.obj_err = self.update_list(ax,self.xyerrs,self.obj_err,"Error")
            plt.pause(1e-4)

        self.sample_action()

        if self.args.figure:
            # update part of figure after getting action
            self.ax_map.set_title('action(%d):%s'%(self.step_count,self.action_str))
            self.save_figure()

    def update_likelihood_rotate(self, map_img, scan_imgs, compute_loss=True):
        map_img = map_img.copy()
        if self.args.flip_map > 0:
            locs = np.random.randint(0, map_img.shape[0], (2, np.random.randint(self.args.flip_map+1)))
            xs = locs[0]
            ys = locs[1]
            map_img[xs,ys]=1-map_img[xs,ys]
            
        time_mark = time.time()        
        if self.perceptual_model == None:
            return self.likelihood
        else:
            likelihood = torch.zeros((self.grid_dirs,self.grid_rows, self.grid_cols),
                                     device=torch.device(self.device), 
                                     dtype=torch.float)

        if self.args.verbose>1: print("update_likelihood_rotate")
        if self.args.ch3=="ZERO":
            input_batch = np.zeros((self.grid_dirs, 3, self.map_rows, self.map_cols))            
            for i in range(self.grid_dirs): # for all orientations
                input_batch[i, 0, :,:] = map_img
                input_batch[i, 1, :,:] = scan_imgs[i,:,:]
                input_batch[i, 2, :,:] = np.zeros_like(map_img)
        elif self.args.ch3=="RAND":
            input_batch = np.zeros((self.grid_dirs, 3, self.map_rows, self.map_cols))            
            for i in range(self.grid_dirs): # for all orientations
                input_batch[i, 0, :,:] = map_img
                input_batch[i, 1, :,:] = scan_imgs[i,:,:]
                input_batch[i, 2, :,:] = np.random.random(map_img.shape)
        else:
            input_batch = np.zeros((self.grid_dirs, 2, self.map_rows, self.map_cols))            
            for i in range(self.grid_dirs): # for all orientations
                input_batch[i, 0, :,:] = map_img
                input_batch[i, 1, :,:] = scan_imgs[i,:,:]
        input_batch = torch.from_numpy(input_batch).float()
        output = self.perceptual_model.forward(input_batch)
        output_softmax  = F.softmax(output.view([1,-1])/self.args.temperature, dim= 1) # shape (1,484)

        if self.args.n_lm_grids !=  self.args.n_local_grids:
            # LM output size != localization space size: adjust LM output to fit to localization space.
            nrows = self.args.n_lm_grids #self.grid_rows/self.args.sub_resolution
            ncols = self.args.n_lm_grids #self.grid_cols/self.args.sub_resolution
            like = output_softmax.cpu().detach().numpy().reshape((self.grid_dirs, nrows, ncols))
            for i in range(self.grid_dirs):
                likelihood[i,:,:] = torch.tensor(cv2.resize(like[i,:,:], (self.grid_rows,self.grid_cols))).float().to(self.device)
            likelihood /= likelihood.sum()
        else:
            likelihood = output_softmax.reshape(likelihood.shape)


        self.lm_time = time.time()-time_mark
        print ("[TIME for LM] %.2f sec"%(self.lm_time))
        del output_softmax, input_batch, output        
        if compute_loss:
            self.compute_loss(likelihood)
        return likelihood
        # self.likelihood = torch.clamp(self.likelihood, 1e-9, 1.0)
        # self.likelihood = self.likelihood/self.likelihood.sum()

    def compute_loss(self, likelihood):
        gtl = torch.tensor(self.gt_likelihood).float().to(self.device)
        if self.args.pm_loss == "KL":
            self.loss_ll = (gtl * torch.log(gtl/likelihood)).sum()            
        elif self.args.pm_loss == "L1":
            self.loss_ll = torch.abs(likelihood - gtl).sum()

        if self.args.update_pm_by=="GTL" or self.args.update_pm_by=="BOTH":
            if len(self.loss_likelihood) < self.args.pm_batch_size:
                self.loss_likelihood.append(self.loss_ll)
                if self.args.verbose > 2:
                    print ("loss_likelihood", len(self.loss_likelihood))
            if len(self.loss_likelihood) >= self.args.pm_batch_size:
                self.back_prop_pm()
                self.loss_likelihood = []

        del gtl
                

    def mask_likelihood(self):
        the_mask = torch.tensor(np.ones([self.grid_dirs, self.grid_rows, self.grid_cols])).float().to(self.device)
        for i in range(self.grid_rows):
            for j in range(self.grid_cols):
                if self.map_for_pose[i, j]>0.5:
                    the_mask[:,i,j]=0.0
        self.likelihood = self.likelihood * the_mask
        #self.likelihood = torch.clamp(self.likelihood, 1e-9, 1.0)
        self.likelihood = self.likelihood/self.likelihood.sum()

        
    def product_belief(self):
        if self.args.verbose>1: print("product_belief")

        if self.args.use_gt_likelihood :
            # gt = torch.from_numpy(self.gt_likelihood/self.gt_likelihood.sum()).float().to(self.divice)
            gt = torch.tensor(self.gt_likelihood).float().to(self.device)
            self.belief = self.belief * (gt)
            #self.belief = self.belief * (self.gt_likelihood)
        else:
            self.belief = self.belief * (self.likelihood)
        #normalize belief
        self.belief /= self.belief.sum()
        #update bel_grid
        guess = np.unravel_index(np.argmax(self.belief.cpu().detach().numpy(), axis=None), self.belief.shape)
        self.bel_grid = Grid(head=guess[0],row=guess[1],col=guess[2])


    
    def do_the_honors(self, pose, belief):
        scan_data = self.get_virtual_lidar(pose)
        scan_2d, _ = self.get_scan_2d_n_headings(scan_data, self.xlim, self.ylim)
        if self.args.use_gt_likelihood:
            gtl = self.get_gt_likelihood_cossim(self.scans_over_map, scan_data)            
            likelihood = softmax(gtl, self.args.temperature)
            likelihood = torch.tensor(likelihood).float().to(self.device)
        else:
            likelihood = self.update_likelihood_rotate(self.map_for_LM,
                                                       scan_2d,
                                                       compute_loss=False)
        bel = belief * likelihood
        bel /= bel.sum()
        new_bel_ent = float((bel * torch.log(bel)).sum())
        return new_bel_ent - self.bel_ent
        
        
    def get_markov_action(self):
        max_ent_diff = -np.inf
        sampled_action_str = ""
        # update belief entropy
        self.bel_ent = (self.belief * torch.log(self.belief)).sum().detach()
        fwd_collision = self.collision_fnc(0, 0, 0, self.scan_2d_slide)
        if fwd_collision:
            action_space = ['turn_left','turn_right']
        else:
            action_space = ['turn_left','turn_right','go_fwd']
            
        for afp, action_str in enumerate(action_space):
            virtual_target = self.get_virtual_target_pose(action_str)
            ### transit the belief according to the action
            bel = self.belief.cpu().detach().numpy() # copy current belief into numpy
            bel = self.trans_bel(bel, action_str)  # transition off the actual trajectory
            bel = torch.from_numpy(bel).float().to(self.device)#$ requires_grad=True)
            ent_diff = self.do_the_honors(virtual_target, bel)
            if ent_diff > max_ent_diff:
                max_ent_diff = ent_diff
                sampled_action_str = action_str
        self.action_str = sampled_action_str
        self.action_from_policy = afp

        
    def get_action(self):
        if self.args.use_aml:
            self.get_markov_action()
            return

        if self.args.verbose>1: print("get_action")
        if self.step_count==0:
            self.cx = torch.zeros(1, 256)
            self.hx = torch.zeros(1, 256)
            # self.cx = Variable(torch.zeros(1, 256))
            # self.hx = Variable(torch.zeros(1, 256))
        else:
            # these are internal states of LSTM. not for back-prop. so, detach them.
            self.cx = self.cx.detach() #Variable(self.cx.data)
            self.hx = self.hx.detach() #Variable(self.hx.data)

        self.scan_2d_low_tensor[0,:,:]=torch.from_numpy(self.scan_2d_low).float().to(self.device)
        # state = torch.cat((self.map_for_RL.detach(), self.belief, self.scan_2d_low_tensor.detach()), dim=0)

        if self.args.n_state_grids == self.args.n_local_grids and self.args.n_state_dirs == self.args.n_headings:
            # no downsample. preserve the path for backprop
            belief_downsample = self.belief
        else:
            belief_downsample = np.zeros((self.args.n_state_dirs, self.args.n_state_grids, self.args.n_state_grids))
            dirs = range(self.bel_grid.head%(self.grid_dirs//self.args.n_state_dirs),self.grid_dirs,self.grid_dirs//self.args.n_state_dirs)
            for i,j in enumerate(dirs):
                bel = self.belief[j,:,:].cpu().detach().numpy()
                bel = cv2.resize(bel, (self.args.n_state_grids,self.args.n_state_grids))#,interpolation=cv2.INTER_NEAREST)
                belief_downsample[i,:,:] = bel
            belief_downsample /= belief_downsample.sum()
            belief_downsample = torch.from_numpy(belief_downsample).float().to(self.device)

        if self.args.n_state_grids == self.args.n_local_grids and self.args.n_state_dirs == self.args.n_headings:
            # no downsample. preserve the path for backprop
            likelihood_downsample = self.likelihood
        else:
            likelihood_downsample = np.zeros((self.args.n_state_dirs, self.args.n_state_grids, self.args.n_state_grids))
            dirs = range(self.bel_grid.head%(self.grid_dirs//self.args.n_state_dirs),self.grid_dirs,self.grid_dirs//self.args.n_state_dirs)
            for i,j in enumerate(dirs):
                lik = self.likelihood[j,:,:].cpu().detach().numpy()
                lik = cv2.resize(lik, (self.args.n_state_grids,self.args.n_state_grids))#,interpolation=cv2.INTER_NEAREST)
                likelihood_downsample[i,:,:] = lik
            likelihood_downsample /= likelihood_downsample.sum()
            likelihood_downsample = torch.from_numpy(likelihood_downsample).float().to(self.device)

        ## map_for_RL : resize it: n_maze_grids --> n_state_grids
        ## scan_2d_low_tensor: n_state_grids

        if self.args.RL_type == 0: 
            state = torch.cat((self.map_for_RL.detach(), 
                               belief_downsample,
                               self.scan_2d_low_tensor.detach()), dim=0)
        elif self.args.RL_type == 1:
            state = torch.cat((belief_downsample,
                               self.scan_2d_low_tensor.detach()), dim=0)
        elif self.args.RL_type == 2:
            state = torch.cat((belief_downsample, likelihood_downsample), dim=0)
            state2 = torch.stack((torch.from_numpy(self.map_for_LM.astype(np.float32)), torch.from_numpy(self.scan_2d_slide.astype(np.float32))), dim=0)


        if self.args.update_pm_by=="BOTH" or self.args.update_pm_by=="RL":
            if self.args.RL_type == 2:
                value, logit, (self.hx, self.cx) = self.policy_model.forward((state.unsqueeze(0), state2.unsqueeze(0), (self.hx, self.cx)))
            else:
                value, logit, (self.hx, self.cx) = self.policy_model.forward((state.unsqueeze(0), (self.hx, self.cx)))            
        else:
            if self.args.RL_type == 2:
                value, logit, (self.hx, self.cx) = self.policy_model.forward((state.detach().unsqueeze(0), state2.detach().unsqueeze(0), (self.hx, self.cx)))
            else:
                value, logit, (self.hx, self.cx) = self.policy_model.forward((state.detach().unsqueeze(0), (self.hx, self.cx)))            

        #state.register_hook(print)
        prob = F.softmax(logit, dim=1)
        log_prob = F.log_softmax(logit, dim=1)
        entropy = -(log_prob * prob).sum(1, keepdim=True)

        if self.optimizer != None:
            self.entropies.append(entropy)
        if self.args.verbose>2:
            print ("entropies", len(self.entropies))

        self.prob=prob.cpu().detach().numpy()
        #argmax for action
        if self.args.action == 'argmax' or self.rl_test:
            action = [[torch.argmax(prob)]]
            action = torch.as_tensor(action)#, device=self.device)
        elif self.args.action == 'multinomial':
            #multinomial sampling for action
            # prob = torch.clamp(prob, 1e-10, 1.0)
            # if self.args.update_rl == False:
            action = prob.multinomial(num_samples=1) #.cpu().detach()
        else:
            raise Exception('action sampling method required')
        
        #action = sample(logit)
        #log_prob = log_prob.gather(1, Variable(action))
        log_prob = log_prob.gather(1, action) 
        #print ('1:%f, 2:%f'%(log_prob.gather(1,action), log_prob[0,action]))
        # if self.args.detach_models == True:
        #     intri_reward = self.intri_model(Variable(state.unsqueeze(0)), action)
        # else:
        #     intri_reward = self.intri_model(state.unsqueeze(0), action)
        # self.intri_rewards.append(intri_reward)

        if self.optimizer != None:
            self.values.append(value)
            self.log_probs.append(log_prob)
        if self.args.verbose > 2:
            print ("values", len(self.values))
            print ("log_probs", len(self.log_probs))
        
        #self.log_probs.append(log_prob[0,action])
        self.action_str = self.action_space[action.item()]
        self.action_from_policy = action.item()

        # now see if the action is safe or valid.it applies only to 'fwd'
        if self.action_str == 'go_fwd' and self.collision_fnc(0, 0, 0, self.scan_2d_slide):
            # then need to chekc collision
            self.collision_attempt = prob[0,2].item()
            # print ('collision attempt: %f'%self.collision_attempt)
            #sample from prob[0,:2]
            self.action_from_policy = prob[0,:2].multinomial(num_samples=1).item()
            self.action_str = self.action_space[self.action_from_policy]
            # print ('action:%s'%self.action_str)
        else:
            self.collision_attempt = 0                    
        
        del state, log_prob, value, action, belief_downsample, entropy, prob

    def navigate(self):
        if not hasattr(self, 'map_to_N'):
            print ('generating maps')
            kernel = np.ones((3,3),np.uint8)
            navi_map = cv2.dilate(self.map_for_LM, kernel, iterations=self.cr_pixels+1)
            if self.args.figure:
                self.ax_map.imshow(navi_map, alpha=0.3)

            self.map_to_N, self.map_to_E, self.map_to_S, self.map_to_W = generate_four_maps(navi_map, self.grid_rows, self.grid_cols)
            
        bel_cell = Cell(self.bel_grid.row, self.bel_grid.col)
        print (self.bel_grid)
        self.target_cell = Cell(self.args.navigate_to[0],self.args.navigate_to[1])
        distance_map = compute_shortest(self.map_to_N,self.map_to_E,self.map_to_S,self.map_to_W, bel_cell, self.target_cell, self.grid_rows)
        print (distance_map)
        shortest_path = give_me_path(distance_map, bel_cell, self.target_cell, self.grid_rows)
        action_list = give_me_actions(shortest_path, self.bel_grid.head)
        self.action_from_policy = action_list[0]
        print ('actions', action_list)
        if self.next_action is None:
            self.action_str = self.action_space[self.action_from_policy]
        else:
            self.action_from_policy = self.next_action
            self.action_str = self.action_space[self.next_action]
            self.next_action = None
            
        if self.action_str == 'go_fwd' and  self.collision_fnc(0, 0, 0, self.scan_2d_slide):
            self.action_from_policy = np.random.randint(2)
            self.action_str = self.action_space[self.action_from_policy]
            self.next_action = 2
        else:
            self.next_action = None
            
        if self.action_str == "hold":
            self.skip_to_end = True
            self.step_count = self.step_max -1

        markerArray = MarkerArray()
        for via_id, via in enumerate(shortest_path):
            marker = Marker()
            marker.header.frame_id = "map"
            marker.type = marker.SPHERE
            marker.action = marker.ADD
            marker.scale.x = 0.2
            marker.scale.y = 0.2
            marker.scale.z = 0.2
            marker.color.a = 1.0
            marker.color.r = 1.0
            marker.color.g = 1.0
            marker.color.b = 1.0        
            marker.pose.orientation.w = 1.0

            marker.pose.position.x = - to_real(via.y, self.ylim, self.grid_cols)
            marker.pose.position.y = to_real(via.x, self.xlim, self.grid_rows)
            
            marker.pose.position.z = 0        
            marker.id = via_id
            markerArray.markers.append(marker)
            
        self.pub_dalpath.publish(markerArray)
        

    def sample_action(self):
        if self.args.manual_control:
            action = -1
            while action < 0:
                print ("suggested action: %s"%self.action_str)
                if self.args.num_actions == 4:
                    keyin = raw_input ("[f]orward/[l]eft/[r]ight/[h]old/[a]uto/[c]ontinue/[n]ext_ep/[q]uit: ")
                elif self.args.num_actions == 3:
                    keyin = raw_input ("[f]orward/[l]eft/[r]ight/[a]uto/[c]ontinue/[n]ext_ep/[q]uit: ")
                if keyin == "f": 
                    action = 2
                elif keyin == "l": 
                    action = 0
                elif keyin == "r": 
                    action = 1
                elif keyin == "h" and self.args.num_actions == 4:
                    action = 3
                elif keyin == "a":
                    action = self.action_from_policy
                elif keyin == "c":
                    self.args.manual_control = False
                    action = self.action_from_policy
                elif keyin == "n":
                    self.skip_to_end = True
                    self.step_count = self.step_max-1
                    action = self.action_from_policy
                elif keyin == "q":
                    self.quit_sequence()
            self.action_idx = action
            self.action_str = self.action_space[self.action_idx]
        else:
            self.action_idx = self.action_from_policy
            self.action_str = self.action_space[self.action_idx]

    def quit_sequence(self):
        self.wrap_up()
        if self.args.jay1 or self.args.gazebo:
            rospy.logwarn("Quit")
            rospy.signal_shutdown("Quit")
        exit()


    def get_virtual_target_pose(self, action_str):
        start_pose = Pose2d(0,0,0)
        start_pose.x = self.believed_pose.x
        start_pose.y = self.believed_pose.y
        start_pose.theta = self.believed_pose.theta

        goal_pose = Pose2d(0,0,0)
        offset = self.heading_resol*self.args.rot_step        
        if action_str == "turn_right":
            goal_pose.theta = wrap(start_pose.theta-offset)
            goal_pose.x = start_pose.x
            goal_pose.y = start_pose.y
        elif action_str == "turn_left":
            goal_pose.theta = wrap(start_pose.theta+offset)
            goal_pose.x = start_pose.x
            goal_pose.y = start_pose.y
        elif action_str == "go_fwd":
            goal_pose.x = start_pose.x + math.cos(start_pose.theta)*self.fwd_step_meters
            goal_pose.y = start_pose.y + math.sin(start_pose.theta)*self.fwd_step_meters
            goal_pose.theta = start_pose.theta
        elif action_str == "hold":
            return start_pose
        else:
            print('undefined action name %s'%action_str)
            exit()

        return goal_pose

        
    def update_target_pose(self):
        self.last_pose.x = self.perturbed_goal_pose.x
        self.last_pose.y = self.perturbed_goal_pose.y
        self.last_pose.theta = self.perturbed_goal_pose.theta

        self.start_pose.x = self.believed_pose.x
        self.start_pose.y = self.believed_pose.y
        self.start_pose.theta = self.believed_pose.theta
        
        offset = self.heading_resol*self.args.rot_step        
        if self.action_str == "turn_right":

            self.goal_pose.theta = wrap(self.start_pose.theta-offset)
            self.goal_pose.x = self.start_pose.x
            self.goal_pose.y = self.start_pose.y
        elif self.action_str == "turn_left":

            self.goal_pose.theta = wrap(self.start_pose.theta+offset)
            self.goal_pose.x = self.start_pose.x
            self.goal_pose.y = self.start_pose.y
        elif self.action_str == "go_fwd":
            self.goal_pose.x = self.start_pose.x + math.cos(self.start_pose.theta)*self.fwd_step_meters
            self.goal_pose.y = self.start_pose.y + math.sin(self.start_pose.theta)*self.fwd_step_meters
            self.goal_pose.theta = self.start_pose.theta
        elif self.action_str == "hold":
            return
        else:
            print('undefined action name %s'%self.action_str)
            exit()
            
        delta_x, delta_y = 0,0
        delta_theta = 0
        if self.args.process_error[0]>0 or self.args.process_error[1]>0:
            delta_x, delta_y = np.random.normal(scale=self.args.process_error[0],size=2)
            delta_theta =  np.random.normal(scale=self.args.process_error[1])

        if self.args.verbose > 1:
            print ('%f, %f, %f'%(delta_x, delta_y, math.degrees(delta_theta)))
        self.perturbed_goal_pose.x = self.goal_pose.x+delta_x
        self.perturbed_goal_pose.y = self.goal_pose.y+delta_y
        self.perturbed_goal_pose.theta = wrap(self.goal_pose.theta+delta_theta)


    def collision_fnc(self, x, y, rad, img):
        corner0 = [x+rad,y+rad]
        corner1 = [x-rad,y-rad]
        x0 = to_index(corner0[0], self.map_rows, self.xlim)
        y0 = to_index(corner0[1], self.map_cols, self.ylim)
        x1 = to_index(corner1[0], self.map_rows, self.xlim)
        y1 = to_index(corner1[1], self.map_cols, self.ylim)

        if x0 < 0 :
            return True
        if y0 < 0:
            return True
        if x1 >= self.map_rows:
            return True
        if y1 >= self.map_cols:
            return True
        # x0 = max(0, x0)
        # y0 = max(0, y0)
        # x1 = min(self.map_rows-1, x1)
        # y1 = min(self.map_cols-1, y1)
        if rad == 0:
            if img[x0, y0] > 0.5 :
                return True
            else:
                return False
        else:
            pass

        for ir in range(x0,x1+1):
            for ic in range(y0,y1+1):
                dx = to_real(ir, self.xlim, self.map_rows) - x
                dy = to_real(ic, self.ylim, self.map_cols) - y
                dist = np.sqrt(dx**2+dy**2)
                if dist <= rad and img[ir,ic]==1.0:
                    return True
        return False


    def collision_check(self):
        row=to_index(self.perturbed_goal_pose.x, self.grid_rows, self.xlim)
        col=to_index(self.perturbed_goal_pose.y, self.grid_cols, self.ylim)

        x = self.perturbed_goal_pose.x
        y = self.perturbed_goal_pose.y
        rad = self.collision_radius

        if self.args.collision_from == "scan" and self.action_str == "go_fwd":
            self.collision = self.collision_fnc(0, 0, 0, self.scan_2d_slide)
        elif self.args.collision_from == "map":
            self.collision = self.collision_fnc(x,y,rad, self.map_for_LM)
        else:
            self.collision = False

        if self.collision:
            self.collision_pose.x = self.perturbed_goal_pose.x
            self.collision_pose.y = self.perturbed_goal_pose.y
            self.collision_pose.theta = self.perturbed_goal_pose.theta
            self.collision_grid.row = row
            self.collision_grid.col = col
            self.collision_grid.head = self.true_grid.head

        if self.collision:
            #undo update target
            self.perturbed_goal_pose.x = self.last_pose.x
            self.perturbed_goal_pose.y = self.last_pose.y
            self.perturbed_goal_pose.theta = self.last_pose.theta


    def get_virtual_lidar(self, current_pose):
        ranges = self.get_a_scan(current_pose.x, current_pose.y, offset=current_pose.theta, fov=True)
        bearing_deg = np.arange(360.0)
        mindeg=0
        maxdeg=359
        incrementdeg=1
        params = {'ranges': ranges,
                  'angle_min': math.radians(mindeg),
                  'angle_max': math.radians(maxdeg),
                  'range_min': self.min_scan_range,
                  'range_max': self.max_scan_range}
                  
        scan_data = Lidar(**params)
        return scan_data

            
    def get_lidar(self, raw=True):

        mindeg=0
        maxdeg=359
        
        if self.args.gazebo:
            params = {'ranges': self.raw_scan.ranges,
                      'angle_min': self.raw_scan.angle_min,
                      'angle_max': self.raw_scan.angle_max,
                      'range_min': self.raw_scan.range_min,
                      'range_max': self.raw_scan.range_max}
        elif self.args.jay1:
            if raw:
                ranges = self.raw_scan.ranges
            else:
                ranges = self.saved_ranges
            params = {'ranges': ranges, #self.raw_scan.ranges,
                      'angle_min': self.raw_scan.angle_min,
                      'angle_max': self.raw_scan.angle_max,
                      'range_min': self.raw_scan.range_min,
                      'range_max': self.raw_scan.range_max}
        else:
            ranges = self.get_a_scan(self.current_pose.x, self.current_pose.y, 
                                     offset=self.current_pose.theta, 
                                     noise=self.args.lidar_noise)
            # bearing_deg = np.arange(360.0)
            # incrementdeg=1
            params = {'ranges': ranges,
                      'angle_min': math.radians(mindeg),
                      'angle_max': math.radians(maxdeg),
                      'range_min': self.min_scan_range,
                      'range_max': self.max_scan_range}
                  
        self.scan_data = Lidar(**params)
        
        if self.args.gazebo or self.args.jay1:
            if raw:
                ranges = self.raw_scan.ranges
            else:
                ranges = self.saved_ranges
            params = {'ranges': ranges, #self.raw_scan.ranges,
                      'angle_min': self.raw_scan.angle_min,
                      'angle_max': self.raw_scan.angle_max,
                      'range_min': self.raw_scan.range_min,
                      'range_max': self.raw_scan.range_max}
            ## it's same as the actual scan.
            self.scan_data_at_unperturbed = Lidar(**params)
        else:
            ## scan_data @ unperturbed pose
            x = to_real(self.true_grid.row, self.xlim, self.grid_rows)
            y = to_real(self.true_grid.col, self.ylim, self.grid_cols)
            offset = self.heading_resol*self.true_grid.head
            ranges = self.get_a_scan(x, y, offset=offset, noise=0)
            params = {'ranges': ranges,
                      'angle_min': math.radians(mindeg),
                      'angle_max': math.radians(maxdeg),
                      'range_min': self.min_scan_range,
                      'range_max': self.max_scan_range}
            
            self.scan_data_at_unperturbed = Lidar(**params)


    def get_lidar_bottom(self):

        params = {'ranges': self.raw_scan_bottom.ranges,
                  'angle_min': self.raw_scan_bottom.angle_min,
                  'angle_max': self.raw_scan_bottom.angle_max,
                  'range_min': self.raw_scan_bottom.range_min,
                  'range_max': self.raw_scan_bottom.range_max}
        self.scan_data_bottom = Lidar(**params)
            
        
    def fwd_clear(self):
        robot_width = 2*self.collision_radius
        safe_distance = 0.05 + self.collision_radius
        left_corner = (wrap_2pi(np.arctan2(self.collision_radius, safe_distance)))
        right_corner = (wrap_2pi(np.arctan2(-self.collision_radius, safe_distance)))
        angles = self.scan_data.angles_2pi
        ranges = self.scan_data.ranges_2pi[(angles < left_corner) | (angles > right_corner)]
        ranges = ranges[(ranges != np.nan) & (ranges != np.inf) ]
        
        if ranges.size == 0:
            return True
        else:
            pass
        
        val = np.min(ranges)

        if val > safe_distance:
            return True
        else:
            print ('top',val)
            rospy.logwarn("Front is Not Clear ! (Top Scan)")
            return False

    def fwd_clear_bottom(self):
        if self.scan_bottom_ready:
            self.scan_bottom_ready = False
        else:
            return True
        safe_distance = 0.20
        fwd_margin=safe_distance
        robot_rad = self.collision_radius
        left_corner = (wrap_2pi(np.arctan2(robot_rad, safe_distance)))
        right_corner = (wrap_2pi(np.arctan2(-robot_rad, safe_distance)))
        angles = self.scan_data_bottom.angles_2pi
        ranges = self.scan_data_bottom.ranges_2pi[(angles < left_corner) | (angles > right_corner)]
        # ranges = self.scan_data_bottom.ranges_2pi
        ranges = ranges[(ranges != np.nan) & (ranges != np.inf) ]
        if ranges.size == 0:
            return True
        else:
            pass
        
        val = np.min(ranges)

        if val > safe_distance:
            return True
        else:
            print ('bot',val)
            rospy.logwarn("Front is Not Clear ! (Bottom)")
            return False


    def execute_action_teleport(self):
        if self.args.verbose>1: print("execute_action_teleport")
        if self.collision: 
            return False
        # if self.action_str == "go_fwd_blocked":
        #     return True

        # if self.args.perturb > 0:
        #     self.turtle_pose_msg.position.x = self.perturbed_goal_pose.x
        #     self.turtle_pose_msg.position.y = self.perturbed_goal_pose.y
        #     yaw = self.perturbed_goal_pose.theta
        # else:
        #     self.turtle_pose_msg.position.x = self.goal_pose.x
        #     self.turtle_pose_msg.position.y = self.goal_pose.y
        #     yaw = self.goal_pose.theta

        # self.turtle_pose_msg.orientation = geometry_msgs.msg.Quaternion(*tf_conversions.transformations.quaternion_from_euler(0, 0, yaw))

        self.teleport_turtle()

        return True


    def transit_belief(self):
        if self.args.verbose>1: print("transit_belief")
        self.belief = self.belief.cpu().detach().numpy()
        if self.collision == True:
            self.belief = torch.from_numpy(self.belief).float().to(self.device)
            return
        self.belief=self.trans_bel(self.belief, self.action_str)
        self.belief = torch.from_numpy(self.belief).float().to(self.device)#$ requires_grad=True)
        
        
    def trans_bel(self, bel, action):
        rotation_step = self.args.rot_step

        if action == "turn_right":
            bel=np.roll(bel,-rotation_step, axis=0)
        elif action == "turn_left":
            bel=np.roll(bel, rotation_step, axis = 0)
        elif action == "go_fwd":
            if self.args.trans_belief == "roll":
                i=0
                bel[i,:,:]=np.roll(bel[i,:,:], -1, axis=0)
                i=1
                bel[i,:,:]=np.roll(bel[i,:,:], -1, axis=1)
                i=2
                bel[i,:,:]=np.roll(bel[i,:,:], 1, axis=0)
                i=3
                bel[i,:,:]=np.roll(bel[i,:,:], 1, axis=1)

            elif self.args.trans_belief == "stoch-shift" or self.args.trans_belief == "shift":
                prior = bel.min()
                for i in range(self.grid_dirs):
                    theta = i * self.heading_resol
                    fwd_dist = self.args.fwd_step
                    dx = fwd_dist*np.cos(theta+np.pi)
                    dy = fwd_dist*np.sin(theta+np.pi)
                    # simpler way:
                    DX = np.round(dx)
                    DY = np.round(dy)
                    shft_hrz = shift(bel[i,:,:], int(DY), axis=1, fill=prior)
                    bel[i,:,:]=shift(shft_hrz, int(DX), axis=0, fill=prior)

        if self.args.trans_belief == "stoch-shift" and action != "hold":
            for ch in range(self.grid_dirs):
                bel[ch,:,:] = ndimage.gaussian_filter(bel[ch,:,:], sigma=self.sigma_xy)
            
            n_dir = self.grid_dirs//4
            p_roll = 0.20
            roll_n = []
            roll_p = []
            for r in range(1, n_dir):
                if roll_n == [] and roll_p == []:
                    roll_n.append(p_roll*np.roll(bel,-1,axis=0))
                    roll_p.append(p_roll*np.roll(bel, 1,axis=0))
                else:
                    roll_n.append(p_roll*np.roll(roll_n[-1],-1,axis=0))
                    roll_p.append(p_roll*np.roll(roll_p[-1], 1,axis=0))
            bel = sum(roll_n + roll_p)+bel                    
        bel /= np.sum(bel)
        return bel

        
    def get_reward(self):
        self.xyerrs.append(self.get_manhattan(self.belief.cpu().detach().numpy(), ignore_hd = True) )
        self.manhattan = self.get_manhattan(self.belief.cpu().detach().numpy(), ignore_hd = False) #manhattan distance between gt and belief.
        self.manhattans.append(self.manhattan)
        if self.args.verbose > 2:
            print ("manhattans", len(self.manhattans))
        
        self.reward = 0.0
        self.reward_vector = np.zeros(5)
        # if self.args.penalty_for_block and self.action_str == "go_fwd_blocked":
        if self.args.penalty_for_block != 0: # and self.collision == True:
            self.reward_vector[0] -= self.args.penalty_for_block * self.collision_attempt
            self.reward += -self.args.penalty_for_block * self.collision_attempt
        if self.args.rew_explore and self.new_pose: # and self.collision_attempt==0:
            self.reward_vector[1] += 1.0            
            self.reward += 1.0
        if self.args.rew_bel_new and self.new_bel: # and self.collision_attempt==0:
            self.reward_vector[1] += 1.0
            self.reward += 1.0
        if self.args.rew_bel_gt: # and self.collision_attempt==0:
            N = self.grid_dirs*self.grid_rows*self.grid_cols
            self.reward_vector[2] += torch.log(N*self.belief[self.true_grid.head,self.true_grid.row,self.true_grid.col]).item() #detach().cpu().numpy()
            self.reward += torch.log(N*self.belief[self.true_grid.head,self.true_grid.row,self.true_grid.col]).item() #.data #detach().cpu().numpy()

        if self.args.rew_bel_gt_nonlog: # and self.collision_attempt==0:
            self.reward_vector[2] += self.belief[self.true_grid.head,self.true_grid.row,self.true_grid.col].item()#detach().cpu().numpy()
            self.reward += self.belief[self.true_grid.head, self.true_grid. row,self.true_grid.col].item()#detach().cpu().numpy()

        if self.args.rew_KL_bel_gt: # and self.collision_attempt==0:
            bel_gt = self.belief[self.true_grid.head, self.true_grid.row, self.true_grid.col].item()#detach().cpu().numpy()
            N = self.grid_dirs*self.grid_rows*self.grid_cols
            new_bel_gt = 1.0/N * np.log(N*np.clip(bel_gt,1e-9,1.0))
            self.reward_vector[2] += new_bel_gt
            self.reward += new_bel_gt #torch.Tensor([new_bel_gt])

        if self.args.rew_infogain: # and self.collision_attempt==0:
            #entropy = -p*log(p)
            # reward = -entropy, low entropy
            bel = torch.clamp(self.belief, 1e-9, 1.0)
            # info gain = p*log(p) - q*log(q)
            # bel=self.belief
            # info_gain = (bel * torch.log(bel)).sum().detach() - self.bel_ent
            new_bel_ent = float((bel * torch.log(bel)).sum())
            info_gain = new_bel_ent - self.bel_ent
            self.bel_ent = new_bel_ent
            self.reward += info_gain 
            self.reward_vector[3] += info_gain 

        if self.args.rew_bel_ent: # and self.collision_attempt==0:
            #entropy = -p*log(p)
            # reward = -entropy, low entropy
            # bel = torch.clamp(self.belief, 1e-9, 1.0)
            bel=self.belief
            self.reward += (bel * torch.log(bel)).sum().item() #detach().cpu().numpy()
            self.reward_vector[3] += (bel * torch.log(bel)).sum().item() #detach().cpu().numpy()

        if self.args.rew_hit: # and self.collision_attempt==0:
            self.reward += 1 if self.manhattan==0 else 0
            self.reward_vector[4] += 1 if self.manhattan==0 else 0
        if self.args.rew_dist: # and self.collision_attempt==0:
            self.reward += (self.longest-self.manhattan)/self.longest
            self.reward_vector[4] = (self.longest-self.manhattan)/self.longest
        if self.args.rew_inv_dist: # and self.collision_attempt==0:
            self.reward += 1.0/(self.manhattan+1.0)
            self.reward_vector[4] = 1.0/(self.manhattan+1.0)

        self.reward = float(self.reward)
        
        self.rewards.append(self.reward)
        if self.args.verbose > 2:
            print ("rewards", len(self.rewards))

        if np.isnan(self.reward):
            raise Exception('reward=nan')
        if self.args.verbose > 1:
            print ('reward=%f'%self.reward)


    def get_euclidean(self):
        return np.sqrt((self.believed_pose.x - self.current_pose.x)**2+(self.believed_pose.y - self.current_pose.y)**2)
    
    def get_manhattan(self, bel, ignore_hd = False):
        # guess = np.unravel_index(np.argmax(bel, axis=None), bel.shape)
        guess = (self.bel_grid.head, self.bel_grid.row, self.bel_grid.col)
        #[self.bel_grid.head,self.bel_grid.x, self.bel_grid.y]
        e_dir = abs(guess[0]-self.true_grid.head)
        e_dir = min(self.grid_dirs-e_dir, e_dir)
        if ignore_hd:
            e_dir = 0
        return float(e_dir+abs(guess[1]-self.true_grid.row)+abs(guess[2]-self.true_grid.col))


    def back_prop(self):
        if self.args.use_aml:
            return
        
        if self.optimizer == None:
            return
        
        if self.args.verbose>1:
            print("back_prop")
        self.Ret = torch.zeros(1,1).detach() 
        self.values.append(self.Ret)
        if self.args.verbose > 2:
            print ("values:", len(self.values))
            print ("rewards:", len(self.rewards))
            print ("log_probs:", len(self.log_probs))
        
        policy_loss = 0
        value_loss = 0
        gae = torch.zeros(1,1).detach()      #Generalized advantage estimate
        #gae = 0

        for i in reversed(range(len(self.rewards))):
            self.Ret = self.gamma * self.Ret + self.rewards[i]
            advantage = self.Ret - self.values[i]
            value_loss = value_loss + 0.5 * advantage.pow(2)
            #Generalized advantage estimate

            delta_t = self.rewards[i] \
                      + self.gamma * self.values[i+1].data\
                      - self.values[i].data
            gae = gae * self.gamma * self.tau + delta_t
            policy_loss = policy_loss - self.log_probs[i] * gae - self.entropy_coef * self.entropies[i]

            #R = self.gamma * R + self.rewards[i] + self.args.lamda * self.intri_rewards[i]
            #advantage = R - self.values[i]
            #value_loss = value_loss + 0.5 * advantage.pow(2)
            
            #delta_t = self.rewards[i] + self.args.lamda * self.intri_rewards[i].data + self.gamma * self.values[i + 1].data - self.values[i].data
            #gae = gae * self.gamma * self.tau + delta_t
            #policy_loss = policy_loss - self.log_probs[i] * Variable(gae) - self.entropy_coef * self.entropies[i]

        ### for logging purpose ###            
        self.loss_policy = policy_loss.item()
        self.loss_value = value_loss.item()
        ###                     ###
        self.optimizer.zero_grad()
        total_loss = policy_loss + self.args.value_loss_coeff * value_loss
        (policy_loss + self.args.value_loss_coeff * value_loss).backward(retain_graph=True)
        torch.nn.utils.clip_grad_norm(self.policy_model.parameters(), self.max_grad_norm)
        # print ('bp grad value')
        # print (self.optimizer.param_groups[0]['params'][0])
        if self.args.schedule_rl:
            self.scheduler_rl.step()

        self.optimizer.step()
        if self.args.verbose>0:
            print ("back_prop (RL) done")

        self.rl_backprop_cnt += 1
        if self.rl_backprop_cnt % self.args.mdl_save_freq == 0 and self.args.update_rl and self.args.save:
            torch.save(self.policy_model.state_dict(), self.rl_filepath)
            print ('RL model saved at %s.'%self.rl_filepath)


    def back_prop_pm(self):
        if self.args.update_pm_by=="GTL" or self.args.update_pm_by=="BOTH":
            self.optimizer_pm.zero_grad()
            (sum(self.loss_likelihood)/float(len(self.loss_likelihood))).backward(retain_graph = True)
            self.optimizer_pm.step()

            mean_test_loss = sum(self.loss_likelihood).item()
            if self.args.schedule_pm:
                # self.scheduler_pm.step(mean_test_loss)
                self.scheduler_pm.step()
            self.pm_backprop_cnt += 1
            if self.args.save and self.pm_backprop_cnt % self.args.mdl_save_freq == 0:
                torch.save(self.perceptual_model.state_dict(), self.pm_filepath)
                print ('perceptual model saved at %s.'%self.pm_filepath)
        else:
            return
        if self.args.verbose>0:
            print ("back_prop_pm done")


    def next_step(self):
        if self.args.verbose>1:
            print ("next_step")
        self.step_count += 1
        if self.args.random_temperature:
            self.args.temperature = 10.0**(-1*np.random.rand())
        if self.step_count >= self.step_max:
            self.next_ep()
        else:
            self.current_state = "update_likelihood"        
            # if self.step_count % 10 == 0:
            #     torch.cuda.empty_cache()
        torch.cuda.empty_cache()
        if self.args.verbose>2:
            print ("max mem alloc", int(torch.cuda.max_memory_allocated()*1e-6))
            print ("max mem cache", int(torch.cuda.max_memory_cached()*1e-6))
            print ("mem alloc", int(torch.cuda.memory_allocated()*1e-6))
            print ("mem cache", int(torch.cuda.memory_cached()*1e-6))

            
    def next_ep(self):
        if not self.rl_test:
            self.back_prop()
            self.flush_all()
        # self.save_tflogs()

        torch.cuda.empty_cache()
        if self.args.figure:
            self.ax_rew.clear()
            self.obj_rew = None
        if self.args.verbose>1:
            print ("next_ep")
        
        # if self.args.verbose > 0:
        #     self.report_status(end_episode=True)

        self.action_from_policy = -1
        self.action_idx = -1
        self.bel_list = []
        self.step_count = 0
        self.collision = False
        # reset belief too
        self.belief[:,:,:]=1.0
        self.belief /= self.belief.sum()#np.sum(self.belief, dtype=float)
        self.bel_ent = (self.belief * torch.log(self.belief)).sum().detach()

        self.acc_epi_cnt +=1
        self.episode_count += 1
        if self.episode_count in range(self.episode_max - self.args.test_ep, self.episode_max):
            self.rl_test = True
        else:
            self.rl_test = False
        if self.episode_count == self.episode_max:
            self.next_env()
        else:
            self.current_state = "new_pose"


    def next_env(self):
        if self.args.verbose>1:
            print ("next_env")

        
        # if not self.rl_test:
        #     self.back_prop()

        
        self.episode_count = 0
        self.env_count += 1
        if self.env_count == self.env_max:
            self.wrap_up()
            exit()
        else:
            self.current_state = "new_env_pose"


    def flush_all(self):
        # reset for back_prop
        self.loss_policy = 0
        self.loss_value = 0
        self.values = []
        self.log_probs = []
        self.rewards = []
        self.manhattans=[]
        self.xyerrs=[]
        self.intri_rewards = []
        self.reward = 0
        self.entropies = []

        
    def wrap_up(self):
        if self.args.save:
            if self.args.verbose > -1:
                print ('output saved at %s'%self.log_filepath)
            # save parameters

            if self.args.update_pm_by != "NONE":
                torch.save(self.perceptual_model.state_dict(), self.pm_filepath)
                print ('perceptual model saved at %s.'%self.pm_filepath)
            if self.args.update_rl:
                torch.save(self.policy_model.state_dict(), self.rl_filepath)
                print ('RL model saved at %s.'%self.rl_filepath)
            if self.args.update_ir:
                torch.save(self.intri_model.state_dict(), self.ir_filepath)
                print ('Intrinsic reward model saved at %s.'%self.ir_filepath)
            #Later to restore:
            # model.load_state_dict(torch.load(filepath))
            # model.eval()
        if self.args.verbose > -1:
            print ('training took %s'%(time.time()-self.start_time))


        
    def save_tflogs(self):
        if self.args.tflog == True:
            #Log scalar values
            info = { 'policy_loss': self.loss_policy,
                     'value_loss': self.loss_value,
                     'pol-val weighted loss': self.loss_policy+self.args.value_loss_coeff*self.loss_value,
                     'discounted_reward': self.Ret.item(),
                     'total_reward': (np.float_(sum(self.rewards))).item(),
                     'likelihood_loss': self.loss_likelihood.item()
            }

            for tag, value in info.items():
                logger.scalar_summary(tag, value, self.episode_count)

            #Log values and gradients of the params (histogram summary)

            if self.args.update_rl:
                for tag, value in self.policy_model.named_parameters():
                    tag = tag.replace('.', '/')
                    logger.histo_summary(tag, value.data.cpu().numpy(), self.episode_count)
                    logger.histo_summary(tag+'/policy_grad', value.grad.data.cpu().numpy(), self.episode_count)
                        
            if self.args.update_pm_by!="NONE":
                for tag, value in self.perceptual_model.named_parameters():
                    tag = tag.replace('.', '/')
                    logger.histo_summary(tag, value.data.cpu().numpy(), self.episode_count)
                    logger.histo_summary(tag+'/perceptual_grad', value.grad.data.cpu().numpy(), self.episode_count)
            if self.args.update_ir:
                for tag, value in self.intri_model.named_parameters():
                    tag = tag.replace('.', '/')
                    logger.histo_summary(tag, value.data.cpu().numpy(), self.episode_count)
                    logger.histo_summary(tag+'/intri_grad', value.grad.data.cpu().numpy(), self.episode_count)


    def cb_active(self):
        rospy.loginfo("Goal pose is now being processed by the Action Server...")


    def cb_feedback(self, feedback):
        #To print current pose at each feedback:
        #rospy.loginfo("Feedback for goal "+str(self.goal_cnt)+": "+str(feedback))
        #rospy.loginfo("Feedback for goal pose received: " + str(feedback))
        pass


    def cb_done(self, status, result):
    # Reference for terminal status values: http://docs.ros.org/diamondback/api/actionlib_msgs/html/msg/GoalStatus.html
        if status == 2:
            rospy.loginfo("Goal pose received a cancel request after it started executing, completed execution!")
            self.move_result_OK = False

        if status == 3:
            rospy.loginfo("Goal pose reached")
            self.move_result_OK = True

        if status == 4:
            rospy.loginfo("Goal pose was aborted by the Action Server")
            self.move_result_OK = False            

        if status == 5:
            rospy.loginfo("Goal pose has been rejected by the Action Server")
            self.move_result_OK = False

        if status == 8:
            rospy.loginfo("Goal pose received a cancel request before it started executing, successfully cancelled!")
            self.move_result_OK = False            

        self.wait_for_move = False
        return

            
    def movebase_client(self):
        goal = MoveBaseGoal()
        goal.target_pose.header.frame_id = "map"
        goal.target_pose.header.stamp = rospy.Time.now() 
        goal.target_pose.pose.position.x = -self.goal_pose.y
        goal.target_pose.pose.position.y = self.goal_pose.x
        q_goal = quaternion_from_euler(0,0, wrap(self.goal_pose.theta+np.pi*0.5))
        
        goal.target_pose.pose.orientation.x = q_goal[0]
        goal.target_pose.pose.orientation.y = q_goal[1]
        goal.target_pose.pose.orientation.z = q_goal[2]
        goal.target_pose.pose.orientation.w = q_goal[3]
        
        rospy.loginfo("Sending goal pose to Action Server")
        rospy.loginfo(str(goal))
        self.wait_for_move = True
        self.client.send_goal(goal, self.cb_done, self.cb_active, self.cb_feedback)


    
    def prep_jay(self):
        # load map, init variables, etc.
        self.clear_objects()
        self.read_map()
        self.make_low_dim_maps()

        if self.args.gtl_off == False:
            # generate synthetic scan data over the map (and directions)
            self.get_synth_scan_mp(self.scans_over_map, map_img=self.map_for_LM, xlim=self.xlim, ylim=self.ylim) 
        self.reset_explored()
        self.update_current_pose_from_robot()
        self.update_true_grid()
        self.sync_goal_to_true_grid()
        if self.args.figure==True:
            self.update_figure(newmap=True)

    def publish_dalscan(self):
        ranges = self.saved_ranges
        ranges[ranges>self.max_scan_range] = np.inf
        ranges=ranges.tolist()
        #np.clip(self.saved_ranges, self.min_scan_range, self.max_scan_range)
        #ranges = tuple(map(tuple, ranges))
        self.raw_scan.ranges = ranges
        self.pub_dalscan.publish(self.raw_scan)
        

    def loop_jay(self, timer_ev): 
        if self.fsm_state == "init":
            # prep jay
            self.prep_jay()
            self.fsm_state = "new_episode"
            if self.args.verbose >= 1:
                print ("[INIT] prep_jay done")
            return

        elif self.fsm_state == "sense":
            # wait for scan
            self.scan_once = True
            self.scan_bottom_once = True
            self.fsm_state = "sensing"
            # if self.args.verbose >= 1:
            #     print ("[SENSE] Wait for scan.")
            self.mark_time = time.time()
            return

        elif self.fsm_state == "sensing":
            if self.scan_ready:
                self.scan_ready = False
                if self.scan_cnt > 10:
                    #publish scan
                    self.publish_dalscan()
                    self.scan_cnt = 0
                    self.fsm_state = "localize"
                    if self.args.verbose >= 1:
                        print ("[SENSING DONE]")
                        print ("[TIME for SENSING] %.3f sec"%(time.time()-self.mark_time))
                else:
                    if self.scan_cnt == 0:
                        self.saved_ranges = np.array(self.raw_scan.ranges)
                    else:
                        self.saved_ranges = np.min(
                            np.stack(
                                (self.saved_ranges,
                                 np.array(self.raw_scan.ranges)),
                                axis = 0),
                            axis = 0)
                    self.scan_cnt += 1
                    self.fsm_state = "sense"
            return

        elif self.fsm_state == "localize":
            if self.args.verbose >= 1:
                print ("[LOCALIZE]")
            # process lidar data
            self.get_lidar(raw=False)
            time_mark = time.time()
            self.do_scan_2d_n_headings()
            print ("[TIME for SCAN TO 2D IMAGE] %.3f sec"%(time.time()-time_mark))
            self.slide_scan()
            # do localization and action sampling
            self.update_explored()
            time_mark = time.time()            
            self.compute_gtl(self.scans_over_map)
            print ("[TIME for GTL] %.2f sec"%(time.time()-time_mark))
            time_mark = time.time()                        
            self.likelihood = self.update_likelihood_rotate(self.map_for_LM, self.scan_2d)

            print ("[TIME for LM] %.2f sec"%(time.time()-time_mark))            
            # if self.collision == False:
            self.product_belief()
            self.update_believed_pose()
            self.update_map_T_odom()
            self.update_bel_list()
            self.get_reward()

            time_mark = time.time()                        
            if self.args.verbose>0:          
                self.report_status(end_episode=False)
            if self.args.figure:             
                self.update_figure()
            if self.save_roll_out:
                self.collect_data()
            print ("logging, saving, figures: %.2f sec"%(time.time()-time_mark))
            ####
            self.fsm_state = "decide"
            # if self.step_count >= self.step_max-1:
            #     self.fsm_state = "end_episode"
            #     self.run_action_module(no_update_fig=True)
            #     # if self.args.random_policy:
            #     #     fwd_collision = self.collision_fnc(0, 0, 0, self.scan_2d_slide)
            #     #     if fwd_collision:
            #     #         num_actions = 2
            #     #     else:
            #     #         num_actions = 3
            #     #     self.action_from_policy = np.random.randint(num_actions)
            #     #     self.action_str = self.action_space[self.action_from_policy]
            #     # else:
            #     #     self.get_action()
            # else:
            #     self.fsm_state = "decide"
            ####

        elif self.fsm_state == "decide":
            # decide move
            if self.args.verbose >= 1:
                print ("[Sample Action]")
            ####
            if self.step_count >= self.step_max-1:
                self.run_action_module(no_update_fig=True)
                self.skip_to_end = True
            else:
                self.run_action_module()
            ####
            if self.skip_to_end:
                self.skip_to_end = False
                self.fsm_state = "end_episode"
            else:
                self.update_target_pose()
                self.fsm_state = "move"

        elif self.fsm_state == "move":
            if self.args.verbose >= 1:
                print ("[MOVE]")
            # do motion control
            self.collision_check()
            if self.collision:
                rospy.logwarn("Front is not clear. Abort action.")
                self.fsm_state = "end_step"
            else:
                if self.args.use_movebase:
                    self.movebase_client()
                else:
                    self.init_motion_control()
                self.fsm_state = "moving"
                self.mark_time = time.time()
                self.wait_for_move = True
                self.scan_on = True
                if self.args.verbose >= 1:
                    print ("[MOVING]")


        elif self.fsm_state == "moving":
            if not self.args.use_movebase:
                self.wait_for_move = self.do_motion_control()
            if self.wait_for_move == False:
                self.fsm_state = "trans-belief"
                self.scan_on = False
                if self.args.verbose >= 1:
                    print ("[DONE MOVING]")
                    print ("[TIME for MOTION] %.3f sec"%(time.time()-self.mark_time))
                

        elif self.fsm_state == "trans-belief":
            if self.args.verbose >= 1:
                print ("[TRANS-BELIEF]")
            self.transit_belief()
            self.fsm_state = "update_true_pose"

        elif self.fsm_state == "update_true_pose":
            self.update_current_pose_from_robot()
            self.update_true_grid()            
            self.fsm_state = "end_step"

        elif self.fsm_state == "end_step":
            self.step_count += 1 # end of step
            self.fsm_state = "sense"
            
        elif self.fsm_state == "end_episode":
            self.end_episode()
            if self.episode_count >= self.episode_max:
                rospy.loginfo("Max episode "+str(self.episode_count)+" reached.")
                self.quit_sequence() # and quit.
            else:
                self.fsm_state = "new_episode"

        elif self.fsm_state == "new_episode":
            self.new_episode()
            self.fsm_state = "spawn"

        elif self.fsm_state == "spawn":
            if self.wait_for_move == False:
                self.fsm_state = "sense"
                self.scan_on = False
                if self.args.verbose >= 1:
                    print ("[DONE MOVING]")
                self.update_current_pose_from_robot()
                self.update_true_grid()            
                    

        else:
            print (self.fsm_state)
            rospy.logerr("Unknown FSM state")
            rospy.signal_shutdown("Unknown FSM state")
            exit()
            
        return


    def end_episode(self):
        if self.args.verbose > 0:
            print ("[END EPISODE]")
        if not self.rl_test:
            self.back_prop()
            self.flush_all()
        if self.args.figure:
            self.ax_rew.clear()
            self.obj_rew = None
        self.acc_epi_cnt +=1
        self.episode_count += 1


    def new_episode(self):
        if self.args.verbose > 0:
            print ("[NEW EPISODE]")
        self.action_from_policy = -1
        self.action_idx = -1
        self.bel_list = []
        self.step_count = 0
        self.scan_cnt = 0
        self.collision = False
        # reset belief too
        self.belief[:,:,:]=1.0
        self.belief /= self.belief.sum()#np.sum(self.belief, dtype=float)
        self.bel_ent = (self.belief * torch.log(self.belief)).sum().detach()

        if self.args.load_init_poses=="none" and self.episode_count==0:
            cnt = 0
            self.init_poses=np.zeros((self.episode_max,3),np.float32)
            while cnt < self.episode_max:
                self.sample_a_pose()
                self.init_poses[cnt,0] = self.goal_pose.x
                self.init_poses[cnt,1] = self.goal_pose.y
                self.init_poses[cnt,2] = self.goal_pose.theta
                cnt += 1
            if self.args.save:
                np.save(os.path.join(self.data_path, 'init_poses.npy'), self.init_poses)
                print (os.path.join(self.data_path, 'init_poses.npy'))
            print ("sample init poses: done")
            print (self.init_poses)
        # load it from saved poses
        elif self.episode_count == 0:
            self.init_poses = np.load(self.args.load_init_poses)
            
        self.goal_pose.x = to_real(to_index(self.init_poses[self.episode_count, 0], self.grid_rows, self.xlim), self.xlim, self.grid_rows)
        self.goal_pose.y = to_real(to_index(self.init_poses[self.episode_count, 1], self.grid_cols, self.ylim), self.ylim, self.grid_cols)
        self.goal_pose.theta = self.init_poses[self.episode_count, 2]            

        if self.args.set_init_pose is not None:
            self.goal_pose.x = to_real(self.args.set_init_pose[0], self.xlim, self.grid_rows)
            self.goal_pose.y = to_real(self.args.set_init_pose[1], self.ylim, self.grid_rows)
            self.goal_pose.theta = 0
            
        # move_base()
        self.movebase_client()
        self.save_roll_out = self.args.save & np.random.choice([False, True], p=[1.0-self.args.prob_roll_out, self.args.prob_roll_out])
        if self.save_roll_out:
            #save roll-out for next episode.
            self.roll_out_filepath = os.path.join(self.log_dir, 'roll-out-%03d-%03d.txt'%(self.env_count,self.episode_count))


    def cbScanTop(self, laser_scan_stuff):
        # print("got in cbScan")
        # if self.wait_for_scan:
        if self.scan_on or self.scan_once:
            self.raw_scan = laser_scan_stuff
            self.get_lidar(raw=True) # ?
            self.scan_ready = True
            self.scan_once = False
            # self.wait_for_scan = False
            
        return


    def cbScanBottom(self, laser_scan_stuff):
        # print("got in cbScan")
        # if self.wait_for_scan:
        
        if self.scan_on or self.scan_bottom_once:
            self.raw_scan_bottom = laser_scan_stuff
            self.get_lidar_bottom()
            self.scan_bottom_ready = True
            self.scan_bottom_once = False
        return

    

    def cbRobotPose(self, robot_pose):

        self.live_pose.x = robot_pose.pose.position.y
        self.live_pose.y = - robot_pose.pose.position.x
        q_robot = [None]*4
        q_robot[0] = robot_pose.pose.orientation.x
        q_robot[1] = robot_pose.pose.orientation.y
        q_robot[2] = robot_pose.pose.orientation.z
        q_robot[3] = robot_pose.pose.orientation.w
        q_rot = quaternion_from_euler(0,0, -np.pi*.5)
        robot_ori = quaternion_multiply(q_rot, q_robot)
        roll,pitch,yaw = euler_from_quaternion(robot_ori)
        self.live_pose.theta = yaw

        self.robot_pose_ready = True
        return

    def cbOdom(self, odom):
        qtn=odom.pose.pose.orientation
        q_odom = [None]*4
        q_odom[0] = qtn.x
        q_odom[1] = qtn.y
        q_odom[2] = qtn.z
        q_odom[3] = qtn.w
        roll,pitch,yaw=euler_from_quaternion(q_odom) #qtn.w, qtn.x, qtn.y, qtn.z)

        self.odom_pose.x = odom.pose.pose.position.x
        self.odom_pose.y = odom.pose.pose.position.y
        self.odom_pose.theta = yaw
                       
        
    def onShutdown(self):
        rospy.loginfo("[LocalizationNode] Shutdown.")

    def loginfo(self, s):
        rospy.loginfo('[%s] %s' % (self.node_name, s))


if __name__ == '__main__':

    #str_date = datetime.datetime.today().strftime('%Y-%m-%d')
    parser = argparse.ArgumentParser()

    ## GENERAL
    parser.add_argument("-c", "--comment", help="your comment", type=str, default='')
    parser.add_argument("--gazebo", "-gz", action="store_true")
    parser.add_argument("--jay1", "-j1", action="store_true")
    parser.add_argument("--use-movebase", action="store_true")
    
    parser.add_argument("--save-loc", type=str, default=os.environ['TB3_LOG']) #"tb3_anl/logs")
    parser.add_argument("--generate-data", action="store_true")
    parser.add_argument("--n-workers", "-nw", type=int, default=multiprocessing.cpu_count())
    parser.add_argument("--load-init-poses", type=str, default="none")
    parser.add_argument("--set-init-pose", type=float, default=None, nargs=3)

    ## COLLISION
    parser.add_argument("--collision-radius", "-cr", type=float, default=0.25)
    parser.add_argument("--collision-from", type=str, choices=['none','map','scan'], default='map')

    
    ## MAPS, EPISODES, MOTIONS
    parser.add_argument("-n", "--num", help = "num envs, episodes, steps", nargs=3, default=[1,10, 10], type=int)    
    parser.add_argument("--load-map", help = "load an actual map", type=str, default=None)
    parser.add_argument("--distort-map", action="store_true")
    parser.add_argument("--flip-map", help = "flip n pixels 0 <--> 1 in map image", type=int, default=0)
    parser.add_argument("--load-map-LM", help = "load an actual map for LM target", type=str, default=None)
    parser.add_argument("--load-map-RL", help = "load an actual map for RL state", type=str, default=None)
    parser.add_argument("--map-pixel", help = "size of a map pixel in real world (meters)", type=float, default=6.0/224.0)
    #parser.add_argument("--maze-grids-range", type=int, nargs=2, default=[None, None])
    parser.add_argument("--n-maze-grids", type=int, nargs='+', default=[11])
    parser.add_argument("--n-local-grids", type=int, default=11)
    parser.add_argument("--n-state-grids", type=int, default=11)
    parser.add_argument("--n-state-dirs", type=int, default=4)

    parser.add_argument("--RL-type", type=int, default=0, choices=[0,1,2]) 
    # 0: original[map+scan+bel], 1: no map[scan+bel], 2:extended[bel+lik+hd-scan+hd-map]

    parser.add_argument("--n-lm-grids", type=int, default=11)
    parser.add_argument("-sr", "--sub-resolution", type=int, default=1)
    parser.add_argument("--n-headings", "-nh", type=int, default=4)
    parser.add_argument("--rm-cells", help="num of cells to delete from maze", type=int, default=11)
    parser.add_argument("--random-rm-cells", type=int, nargs=2, default=[0,0])
    parser.add_argument("--backward-compatible-maps","-bcm", action="store_true")
    parser.add_argument("--random-thickness", action="store_true")
    parser.add_argument("--thickness", type=float, default=None)
    parser.add_argument("--save-boundary", type=str, choices=['y','n','r'], default='y')

    parser.add_argument("--init-pose", type=int, nargs=3, default=None)

    ## Error Sources:
    ## 1. initial pose - uniform pdf
    ## 2. odometry (or control) - gaussian pdf
    ## 3. use scenario: no error or init error + odom error accumulation
    parser.add_argument("-ie", "--init-error", type=str, choices=['NONE','XY','THETA','BOTH'],default='NONE')
    parser.add_argument("-pe", "--process-error", type=float, nargs=2, default=[0,0])
    parser.add_argument("--fov", help="angles in (fov[0], fov[1]) to be removed", type=float, nargs=2, default=[0, 0])
    parser.add_argument("--lidar-noise", help="number of random noisy rays in a scan", type=int, default=0)
    parser.add_argument("--lidar-sigma", help="sigma for lidar (1d) range", type=float, default=0)
    parser.add_argument("--scan-range", help="[min, max] scan range (m)", type=float, nargs=2, default=[0.10, 3.5])

    ## VISUALIZE INFORMATION
    parser.add_argument("-v", "--verbose", help="increase output verbosity", type=int, default=0, nargs='?', const=1)
    parser.add_argument("-t", "--timer", help="timer period (sec) default 0.1", type=float, default=0.1)
    parser.add_argument("-f", "--figure", help="show figures", action="store_true")
    parser.add_argument("--figure-save-freq", "-fsf", type=int, default=1)
    # parser.add_argument("-p", "--print-map", help="print map", action="store_true")


    ## GPU
    parser.add_argument("-ug", "--use-gpu", action="store_true")
    parser.add_argument("-sg", "--set-gpu", help="set cuda visible devices, default none", type=int, default=[],nargs='+')


    ## MOTION(PROCESS) MODEL
    parser.add_argument('--trans-belief', help='select how to fill after transition', choices=['shift','roll','stoch-shift'], default='stoch-shift', type=str)
    parser.add_argument("--fwd-step", "-fs", type=int, default=1)
    parser.add_argument("--rot-step", "-rs", type=int, default=1)
    parser.add_argument("--sigma-xy", "-sxy", type=float, default=.5)

    ## RL-GENERAL
    parser.add_argument('--update-rl', dest='update_rl', action='store_true')
    parser.add_argument('--no-update-rl', dest='update_rl',help="don't update AC model", action="store_false")
    parser.add_argument('--update-ir', dest='update_ir', action='store_true')
    parser.add_argument('--no-update-ir', dest='update_ir',help="don't update IR model", action="store_false")
    parser.set_defaults(update_rl=False, update_ir=False)
    parser.add_argument('--random-policy', action='store_true')
    parser.add_argument('--navigate-to', type=int, nargs='+', default=None)
    parser.add_argument('--use-aml', action='store_true')

    ## RL-STATE
    parser.add_argument('--binary-scan', action='store_true')

    ## RL-ACTION
    parser.add_argument("--manual-control","-mc", action="store_true")
    parser.add_argument('--num-actions', type=int, default=3)
    parser.add_argument('--test-ep', help='number of test episode at the end of each env', type=int, default=0)
    parser.add_argument('-a','--action', help='select action : argmax or multinomial', choices=['argmax','multinomial'], default='multinomial', type=str)

    ## RL-PARAMS
    parser.add_argument('-lam', '--lamda', help="weight for intrinsic reward, default=0.7", type=float, default=0.7)
    parser.add_argument('-vlcoeff', '--value_loss_coeff', help="value loss coefficient, default=1.0", type=float, default=1.0)
    parser.add_argument('-lr', '--lrrl', help="lr for RL (1e-4)", type=float, default=1e-4)
    parser.add_argument('-cent', '--c-entropy', help="coefficient of entropy in policy loss (0.001)", type=float, default=0.001)

    ## REWARD
    # parser.add_argument('--block-penalty', dest='penalty_for_block', help="penalize for blocked fwd", action="store_true")
    parser.add_argument('--block-penalty', dest='penalty_for_block', help="penalize for blocked fwd", type=float, default=0)
    parser.add_argument('--rew-explore', help="reward for exploration", action="store_true")
    parser.add_argument('--rew-bel-new', help='reward for new belief pose', action="store_true")
    parser.add_argument('--rew-bel-ent', help="reward for low entropy in belief", action="store_true")
    parser.add_argument('--rew-infogain', help="reward for info gain", action="store_true")
    parser.add_argument('--rew-bel-gt-nonlog', help="reward for correct belief", action="store_true")
    parser.add_argument('--rew-bel-gt', help="reward for correct belief", action="store_true")
    parser.add_argument('--rew-KL-bel-gt', help="reward for increasing belief at gt pose", action="store_true")
    parser.add_argument('--rew-dist', help="reward for distance", action="store_true")
    parser.add_argument('--rew-hit', help="reward for distance being 0", action="store_true")
    parser.add_argument('--rew-inv-dist', help="r=1/(1+d)", action="store_true")

    ## TRUE LIKELIHOOD
    parser.add_argument("--gtl-src", help="source of GTL", choices=['hd-cos','hd-corr','hd-corr-clip'], default='hd-cos')
    parser.add_argument("--gtl-output", choices=['softmax','softermax','linear'], default='softmax')
    parser.add_argument("-go", "--gtl-off", action="store_true")

    ## LM-GENERAL
    parser.add_argument("-temp", "--temperature", help="softmax temperature", type=float, default=1.0)
    parser.add_argument("-rt", "--random-temperature", help="softmax temperature", action="store_true")

    parser.add_argument('--pm-net', help ="select PM network",
                        choices = ['none', 'densenet121', 'densenet169', 'densenet201', 'densenet161',
                                   'resnet18', 'resnet50', 'resnet101', 'resnet152',
                                   'resnet18s', 'resnet50s', 'resnet101s', 'resnet152s'],
                        default='none')
    parser.add_argument('--pm-loss', choices=['L1','KL'], default='KL')
    parser.add_argument('--pm-scan-step', type=int, default=1)
    parser.add_argument('--shade', dest="shade", help="shade for scan image", action="store_true")
    parser.add_argument('--no-shade', dest="shade", help="no shade for scan image", action="store_false")
    parser.set_defaults(shade=False)

    parser.add_argument('--pm-batch-size', '-pbs', help='batch size of pm model.', default=10, type=int)

    parser.add_argument("-ugl", "--use-gt-likelihood", help="PM = ground truth likelihood", action="store_true")
    parser.add_argument("--mask", action="store_true", help='mask likelihood with obstacle info')
    parser.add_argument("-ch3","--ch3", choices=['NONE','RAND','ZERO'], type=str, default='NONE')

    parser.add_argument("--n-pre-classes", "-npc", type=int, default=None)
    


    parser.add_argument("--schedule-pm", action="store_true")
    parser.add_argument("--schedule-rl", action="store_true")
    parser.add_argument("--pm-step-size", type=int, default=250)
    parser.add_argument("--rl-step-size", type=int, default=250)
    parser.add_argument("--pm-decay", type=float, default=0.1)
    parser.add_argument("--rl-decay", type=float, default=0.1)
    parser.add_argument("--drop-rate", type=float, default=0.0)

    ## LM-PARAMS
    parser.add_argument('-lp', '--lrpm', help="lr for PM (1e-5)", type=float, default=1e-5)
    parser.add_argument('-upm', '--update-pm-by', help="train PM with GTL,RL,both, none", choices = ['GTL','RL','BOTH','NONE'], default='NONE', type=str)

    ## LOGGING
    parser.add_argument('-ln', "--tflogs-name", help="experiment name to append to the tensor board log files", type=str, default=None)
    parser.add_argument('-tf', '--tflog', dest="tflog",help="tensor board log True/False", action="store_true")
    parser.add_argument('-ntf', '--no-tflog', dest="tflog",help="tensor board log True/False", action="store_false")
    parser.set_defaults(tflog=False)

    parser.add_argument('--save', help="save logs and models", action="store_true", dest='save')
    parser.add_argument('--no-save', help="don't save any logs or models", action="store_false", dest='save')
    parser.set_defaults(save=True)

    parser.add_argument('-pro', '--prob-roll-out', help="sample probability for roll out (0.01)", type=float, default=0.00)

    parser.add_argument('--mdl-save-freq', type=int, default=1)

    ## LOADING MODELS/DATA
    parser.add_argument('--pm-model', help="perceptual model path and file", type=str, default=None)
    parser.add_argument('--use-pretrained', action='store_true')
    parser.add_argument('--rl-model', help="RL model path and file", type=str, default=None)
    parser.add_argument('--ir-model', help="intrinsic reward model path and file", type=str, default=None)
    parser.add_argument('--test-mode', action="store_true")
    parser.add_argument('--test-data-path', type=str, default='')

    parser.add_argument('--ports', type=str, default='')

    args = parser.parse_args()

    
    # if args.suppress_fig:
    #     import matplotlib as mpl
    #     mpl.use('Agg')


    if 360%args.pm_scan_step !=0 or args.pm_scan_step <=0 or args.pm_scan_step > 360:
        raise Exception('pm-scan-step should be in [1, 360]')
    if args.pm_model is not None:
        if os.path.islink(args.pm_model):
            args.pm_model = os.path.realpath(args.pm_model)
    if args.rl_model is not None:
        if os.path.islink(args.rl_model):
            args.rl_model = os.path.realpath(args.rl_model)

    print (args)

    if len(args.set_gpu)>0:
        os.environ["CUDA_VISIBLE_DEVICES"]=','.join(str(x) for x in args.set_gpu)

    

    # while(1): localizer.loop()
    if args.jay1 or args.gazebo:
        rospy.init_node('DAL', anonymous=False)
        localizer = LocalizationNode(args)
        
        rospy.on_shutdown(localizer.onShutdown)
        rospy.spin()
    else:
        localizer = LocalizationNode(args)
        while(1):
            localizer.loop()

