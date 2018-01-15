from __future__ import division, print_function
from glob import glob
import os
import time

import numpy as np
import scipy.misc
import scipy
import tensorflow as tf
import matplotlib.pyplot as plt
from PIL import Image
import lmdb
import io
import sys
from IPython.display import display

import mmd as MMD
import load
from ops import safer_norm
import utils
import pprint
from mmd import _eps, _check_numerics, _debug
from architecture import get_networks
from scorer import Scorer
import pipeline

#from compute_scores import *

class MMD_GAN(object):
    def __init__(self, sess, config, 
                 batch_size=64, output_size=64,
                 z_dim=100, c_dim=3, data_dir='./data'):
        if config.learning_rate_D < 0:
            config.learning_rate_D = config.learning_rate
        """
        Args:
            sess: TensorFlow session
            batch_size: The size of batch. Should be specified before training.
            output_size: (optional) The resolution in pixels of the images. [64]
            z_dim: (optional) Dimension of dim for Z. [100]
            gf_dim: (optional) Dimension of gen filters in first conv layer. [64]
            df_dim: (optional) Dimension of discrim filters in first conv layer. [64]
            gfc_dim: (optional) Dimension of gen units for for fully connected layer. [1024]
            dfc_dim: (optional) Dimension of discrim units for fully connected layer. [1024]
            c_dim: (optional) Dimension of image color. For grayscale input, set to 1. [3]
        """
        self.timer = utils.Timer()
        self.check_numerics = _check_numerics
        self.dataset = config.dataset
        if config.architecture == 'dc128':
            output_size = 128
        elif config.output_size == 128:
            config.architecture = 'dc128'
        if config.architecture in ['dc64', 'dcgan64']:
            output_size = 64
            
        if config.compute_scores:
            self.scorer = Scorer(self.dataset, config.MMD_lr_scheduler)
#            if self.dataset == 'mnist':
#                mod = LeNet()
#                s, f = 100000, 500
#            else:
#                mod = Inception()
#                s, f = 25000, 2000
#            os = '' if (output_size <= 32) else ('-%d' % output_size)
#            path = os.path.join(data_dir, '%s-codes%s.npy' % (self.dataset, os))
#            arr = np.load(path)
#            self.scoring = {'model': mod, 'train_codes': arr[:s], 'output': [], 
#                            'frequency': f, 'size': s}
#            if config.MMD_lr_scheduler:
#                self.scoring['3sample'] = []
#                self.scoring['3sample_chances'] = 0
            
        self.sess = sess

#        elif config.output_size == 64:
#            config.architecture = 'dc64'
        if config.real_batch_size == -1:
            config.real_batch_size = config.batch_size
        self.config = config
        self.is_grayscale = (c_dim == 1)
        self.batch_size = batch_size
        self.real_batch_size = config.real_batch_size
        self.sample_size = 64 if self.config.is_train else batch_size
#        if self.dataset == 'GaussianMix':
#            self.sample_size = min(16 * batch_size, 512)
        self.output_size = output_size
        self.data_dir = data_dir
        self.z_dim = z_dim

        self.gf_dim = config.gf_dim
        self.df_dim = config.df_dim
        self.dof_dim = self.config.dof_dim

        self.c_dim = c_dim            
        
        discriminator_desc = '_dc' if self.config.dc_discriminator else ''
        d_clip = self.config.discriminator_weight_clip
        dwc = ('_dwc_%f' % d_clip) if (d_clip > 0) else ''
        if self.config.learning_rate_D == self.config.learning_rate:
            lr = 'lr%.8f' % self.config.learning_rate
        else:
            lr = 'lr%.8fG%fD' % (self.config.learning_rate, self.config.learning_rate_D)
        arch = '%dx%d' % (self.config.gf_dim, self.config.df_dim)
        
        kernel_name = self.config.kernel
        if self.config.d_kernel != "":
            kernel_name += '-D' + self.config.d_kernel
        self.description = ("%s%s_%s%s_%s%sd%d-%d-%d_%s_%s_%s" % (
                    self.dataset, arch,
                    self.config.architecture, discriminator_desc,
                    kernel_name, dwc, self.config.dsteps,
                    self.config.start_dsteps, self.config.gsteps, self.batch_size,
                    self.output_size, lr))
        
        if self.config.batch_norm:
            self.description += '_bn'
        
        self._ensure_dirs()
        
        if self.config.log:
            self.old_stdout = sys.stdout
            self.old_stderr = sys.stderr
            self.log_file = open(os.path.join(self.sample_dir, 'log.txt'), 'w', buffering=1)
            print('Execution start time: %s' % time.ctime())
            print('Log file: %s' % self.log_file)
            sys.stdout = self.log_file
            sys.stderr = self.log_file
        print('Execution start time: %s' % time.ctime())
        pprint.PrettyPrinter().pprint(self.config.__dict__['__flags'])
        self.build_model()
        
        self.initialized_for_sampling = config.is_train

    def _ensure_dirs(self, folders=['sample', 'log', 'checkpoint']):
        if type(folders) == str:
            folders = [folders]
        if self.config.gradient_penalty > 0:
            if not self.config.gp_type in self.config.suffix:
                self.config.suffix = '_' + self.config.gp_type + self.config.suffix
        for folder in folders:
            ff = folder + '_dir'
            if not os.path.exists(ff):
                os.makedirs(ff)
            self.__dict__[ff] = os.path.join(self.config.__getattr__(ff),
                                             self.config.name + self.config.suffix,
                                             self.description)
            if not os.path.exists(self.__dict__[ff]):
                os.makedirs(self.__dict__[ff])
            

    def build_model(self):
        self.global_step = tf.Variable(0, name="global_step", trainable=False)
        self.lr = tf.Variable(self.config.learning_rate, name='lr', 
                                  trainable=False, dtype=tf.float32)
        self.lr_decay_op = self.lr.assign(tf.maximum(self.lr * self.config.decay_rate, 1.e-6))
        with tf.variable_scope('loss'):
            if self.config.is_train and (self.config.gradient_penalty > 0):
                self.gp = tf.Variable(self.config.gradient_penalty, 
                                      name='gradient_penalty', 
                                      trainable=False, dtype=tf.float32)
                self.gp_decay_op = self.gp.assign(self.gp * self.config.gp_decay_rate)

        self.set_pipeline()

        self.z = tf.random_uniform([self.batch_size, self.z_dim], minval=-1., 
                                   maxval=1., dtype=tf.float32, name='z')
        self.sample_z = tf.constant(np.random.uniform(-1, 1, size=(self.sample_size, 
                                                      self.z_dim)).astype(np.float32),
                                    dtype=tf.float32, name='sample_z')        

        Generator, Discriminator = get_networks(self.config.architecture)
        generator = Generator(self.gf_dim, self.c_dim, self.output_size, self.config.batch_norm)
        self.discriminator = Discriminator(self.df_dim, self.dof_dim, self.config.batch_norm & (self.config.gradient_penalty <= 0))
        # tf.summary.histogram("z", self.z)

        self.G = generator(self.z, self.batch_size)

        
        if self.check_numerics:
            self.G = tf.check_numerics(self.G, 'self.G')
        self.sampler = generator(self.sample_z, self.sample_size)
        
        if self.config.dc_discriminator:
            self.d_images_layers = self.discriminator(self.images, 
                self.real_batch_size, return_layers=True)
            self.d_G_layers = self.discriminator(self.G, self.batch_size,
                                                 return_layers=True)
            self.d_images = self.d_images_layers['hF']
            self.d_G = self.d_G_layers['hF']
        else:
            self.d_images = tf.reshape(self.images, [self.real_batch_size, -1])
            self.d_G = tf.reshape(self.G, [self.batch_size, -1])
        
        if _debug:
            utils.variable_summaries({'Dreal': self.d_images, 'Dfake': self.d_G})
            tf.summary.scalar('norm_Dfake', tf.norm(self.d_G))
            tf.summary.scalar('nomr_Dreal', tf.norm(self.d_images))
            tf.summary.scalar('norm_diff_Dreal_Dfake', tf.norm(self.d_G - self.d_images))
            
        if self.config.is_train:
            self.set_loss(self.d_G, self.d_images)

        block = min(8, int(np.sqrt(self.real_batch_size)), int(np.sqrt(self.batch_size)))
        tf.summary.image("train/input image", 
                         self.imageRearrange(tf.clip_by_value(self.images, 0, 1), block))
        tf.summary.image("train/gen image", 
                         self.imageRearrange(tf.clip_by_value(self.G, 0, 1), block))
        
        t_vars = tf.trainable_variables()

        self.d_vars = [var for var in t_vars if 'd_' in var.name]
        self.g_vars = [var for var in t_vars if 'g_' in var.name]

        self.saver = tf.train.Saver(max_to_keep=2)

        if 'distance' in self.config.Loss_variance:
            self.Loss_variance = utils.Loss_variance(
                self.sess, self.dof_dim, 
                lambda x, bs: self.discriminator(x, bs),
                kernel_name='distance'
            )
            
        print('[*] Model built.')

    def set_loss(self, G, images):
        if self.check_numerics:
            G = tf.check_numerics(G, 'G')
            images = tf.check_numerics(images, 'images')

        kernel = getattr(MMD, '_%s_kernel' % self.config.kernel)
        kerGI = kernel(G, images)
        
        if _debug:
            utils.variable_summaries({'K_XX': kerGI[0], 'K_XY': kerGI[1], 'K_YY': kerGI[2]})
            
        with tf.variable_scope('loss'):
            if self.config.model in ['mmd', 'mmd_gan']:
                self.g_loss = MMD.mmd2(kerGI)
                if self.config.d_kernel != '':
                    DkerGI = getattr(MMD, '_%s_kernel' % self.config.d_kernel)(G, images)
                    self.d_loss = -MMD.mmd2(DkerGI)
                else:
                    self.d_loss = -self.g_loss 
                self.optim_name = 'kernel_loss'
            elif self.config.model == 'tmmd':
                kl, rl, var_est = MMD.mmd2_and_ratio(kerGI)
                self.g_loss = rl
                self.optim_name = 'ratio_loss'
                tf.summary.scalar("kernel_loss", kl)#tf.sqrt(kl + _eps))
                tf.summary.scalar("variance_estimate", var_est)
            
        self.add_gradient_penalty(kernel, G, images)
        self.add_l2_penalty()
        
        print('[*] Loss set')

    def add_gradient_penalty(self, kernel, fake, real):
        bs = min([self.batch_size, self.real_batch_size])
        real, fake = real[:bs], fake[:bs]
        
        if self.config.single_batch_experiment:
            alpha = tf.constant(np.random.rand(bs), dtype=tf.float32, name='const_alpha')
        else:
            alpha = tf.random_uniform(shape=[bs])
        if 'mid' in self.config.suffix:
            alpha = .4 + .2 * alpha
        elif 'edges' in self.config.suffix:
            qq = tf.cast(tf.reshape(tf.multinomial([[.5, .5]], bs),
                                    [bs]), tf.float32)
            alpha = .1 * alpha * qq + (1. - .1 * alpha) * (1. - qq)
        elif 'edge' in self.config.suffix:
            alpha = .95 + .05 * alpha

        if self.config.gp_type == 'feature_space':
            alpha = tf.reshape(alpha, [bs, 1])
            x_hat = (1. - alpha) * real + alpha * fake
            Ekx = lambda yy: tf.reduce_mean(kernel(x_hat, yy, K_XY_only=True), axis=1)
            witness = Ekx(real) - Ekx(fake)
            gradients = tf.gradients(witness, [x_hat])[0]
        elif self.config.gp_type == 'data_space':
            alpha = tf.reshape(alpha, [bs, 1, 1, 1])
            real_data = self.images[:bs] #before discirminator
            fake_data = self.G[:bs] #before discriminator
            x_hat_data = (1. - alpha) * real_data + alpha * fake_data
            if self.check_numerics:
                x_hat_data = tf.check_numerics(x_hat_data, 'x_hat_data')
            x_hat = self.discriminator(x_hat_data, bs)
            if self.check_numerics:
                x_hat = tf.check_numerics(x_hat, 'x_hat')
            Ekx = lambda yy: tf.reduce_mean(kernel(x_hat, yy, K_XY_only=True), axis=1)
            Ekxr, Ekxf = Ekx(real), Ekx(fake)
            witness = Ekxr - Ekxf
            if self.check_numerics:
                witness = tf.check_numerics(witness, 'witness')
            gradients = tf.gradients(witness, [x_hat_data])[0]
            if self.check_numerics:
                gradients = tf.check_numerics(gradients, 'gradients 0')
            if _debug:
                tf.summary.scalar('norm_witness_mid', tf.norm(witness))
                tf.summary.scalar('norm_Ekx_real', tf.norm(Ekxr))
                tf.summary.scalar('norm_Ekx_fake', tf.norm(Ekxf))
        elif self.config.gp_type == 'wgan':
            alpha = tf.reshape(alpha, [bs, 1, 1, 1])
            real_data = self.images #before discirminator
            fake_data = self.G #before discriminator
            x_hat_data = (1. - alpha) * real_data + alpha * fake_data
            x_hat = self.discriminator(x_hat_data, bs)
            gradients = tf.gradients(x_hat, [x_hat_data])[0]
        
        if self.check_numerics:  
            penalty = tf.check_numerics(tf.reduce_mean(tf.square(safer_norm(gradients, axis=1) - 1.0)), 'penalty')
        else:
            penalty = tf.reduce_mean(tf.square(safer_norm(gradients, axis=1) - 1.0))#

        with tf.variable_scope('loss'):
            if self.config.gradient_penalty > 0:
#                self.g_loss = self.mmd_loss
                self.d_loss += penalty * self.gp
                self.optim_name += ' gp %.1f' % self.config.gradient_penalty
                tf.summary.scalar('dx_penalty', penalty)
                print('[*] Gradient penalty added')
            tf.summary.scalar(self.optim_name + ' G', self.g_loss)
            tf.summary.scalar(self.optim_name + ' D', self.d_loss)
        
    def add_l2_penalty(self):
        if self.config.L2_discriminator_penalty > 0:
            penalty = 0.0
            for _, layer in self.d_G_layers.items():
                penalty += tf.reduce_mean(tf.reshape(tf.square(layer), [self.batch_size, -1]), axis=1)
            for _, layer in self.d_images_layers.items():
                penalty += tf.reduce_mean(tf.reshape(tf.square(layer), [self.batch_size, -1]), axis=1)
            self.d_L2_penalty = self.config.L2_discriminator_penalty * tf.reduce_mean(penalty)
            self.d_loss += self.d_L2_penalty
            self.optim_name += ' L2 dp %.6f' % self.config.L2_discriminator_penalty
            tf.summary.scalar('L2_disc_penalty', self.d_L2_penalty)
            print('[*] L2 discriminator penalty added')
        
    def set_grads(self):
        with tf.variable_scope("G_grads"):
            self.g_kernel_optim = tf.train.AdamOptimizer(self.lr, beta1=0.5, beta2=0.9)
            self.g_gvs = self.g_kernel_optim.compute_gradients(
                loss=self.g_loss,
                var_list=self.g_vars
            )       
            self.g_gvs = [(tf.clip_by_norm(gg, 1.), vv) for gg, vv in self.g_gvs]
            self.g_grads = self.g_kernel_optim.apply_gradients(
                self.g_gvs, 
                global_step=self.global_step
            ) # minimizes self.g_loss <==> minimizes MMD
        if self.config.dc_discriminator or ('optme' in self.config.model):
            with tf.variable_scope("D_grads"):
                self.d_kernel_optim = tf.train.AdamOptimizer(
                    self.lr * self.config.learning_rate_D / self.config.learning_rate, 
                    beta1=0.5, beta2=0.9
                )
                self.d_gvs = self.d_kernel_optim.compute_gradients(
                    loss=self.d_loss, 
                    var_list=self.d_vars
                )
                # negative gradients not needed - by definition d_loss = -optim_loss
#                if self.config.gradient_penalty == 0: 
                self.d_gvs = [(tf.clip_by_norm(gg, 1.), vv) for gg, vv in self.d_gvs]
                self.d_grads = self.d_kernel_optim.apply_gradients(self.d_gvs) # minimizes self.d_loss <==> max MMD
                dclip = self.config.discriminator_weight_clip
                if dclip > 0:
                    self.d_vars = [tf.clip_by_value(d_var, -dclip, dclip) 
                                       for d_var in self.d_vars]
        else:
            self.d_grads = None      
        print('[*] Gradients set')
    
    def train_step(self, batch_images=None):
        step = self.sess.run(self.global_step)
        write_summary = ((np.mod(step, 50) == 0) and (step < 1000)) \
                or (np.mod(step, 1000) == 0) or (self.err_counter > 0)

        if (self.g_counter == 0) and (self.d_grads is not None):
            d_steps = self.config.dsteps
            if ((step % 500 == 0) or (step < 20)):
                d_steps = self.config.start_dsteps
            self.d_counter = (self.d_counter + 1) % (d_steps + 1)
        if self.d_counter == 0:
            self.g_counter = (self.g_counter + 1) % self.config.gsteps        
        # write_summary=True

        eval_ops = [self.g_gvs, self.d_gvs, self.g_loss, self.d_loss]
        if self.config.is_demo:
            summary_str, g_grads, d_grads, g_loss, d_loss = self.sess.run(
                [self.TrainSummary] + eval_ops
            )
        else:
            if self.d_counter == 0:
                if write_summary:
                    _, summary_str, g_grads, d_grads, g_loss, d_loss = self.sess.run(
                        [self.g_grads, self.TrainSummary] + eval_ops
                    )
                else:
                    _, g_grads, d_grads, g_loss, d_loss = self.sess.run([self.g_grads] + eval_ops)
            else:
                _, g_grads, d_grads, g_loss, d_loss = self.sess.run([self.d_grads] + eval_ops)
            et = self.timer(step, "g step" if (self.d_counter == 0) else "d step", False)
#        print('[*] Training step: gradients computed.')
        assert ~np.isnan(g_loss), et + "NaN g_loss, epoch: "
        assert ~np.isnan(d_loss), et + "NaN d_loss, epoch: "
        # if G STEP, after D steps
        if self.d_counter == 0:
            if step % 10000 == 0:
                try:
                    self.writer.add_summary(summary_str, step)
                    self.err_counter = 0
                except Exception as e:
                    print('Step %d summary exception. ' % step, e)
                    self.err_counter += 1
            if write_summary:
                self.timer(step, "%s, G: %.8f, D: %.8f" % (self.optim_name, g_loss, d_loss))
                if self.config.L2_discriminator_penalty > 0:
                    print('D_L2 penalty: %.8f' % self.sess.run(self.d_L2_penalty))
                if _debug:
                    print('discriminator output, real[0:3] ', self.sess.run(self.d_images)[:3])
                    print('discriminator output, fake[0:3] ', self.sess.run(self.d_images)[:3])
            if np.mod(step + 1, self.config.max_iteration//5) == 0:
                if not self.config.MMD_lr_scheduler:
#                    self.lr *= self.config.decay_rate
                    self.sess.run(self.lr_decay_op)
                    print('current learning rate: %f' % self.sess.run(self.lr))
                if ('decay_gp' in self.config.suffix) and (self.config.gradient_penalty > 0):
                    self.sess.run(self.gp_decay_op)
                    print('current gradient penalty: %f' % self.sess.run(self.gp))
        
            if self.config.compute_scores:
#                print('[ ] Training step: copmuting scores...')
                self.scorer.compute(self, step)
#                self.compute_scores(step)
            
            if 'distance' in self.config.Loss_variance:
                self.Loss_variance()
        
        return g_loss, d_loss, step
      

    def train_init(self):
        self.set_grads()

        self.sess.run(tf.local_variables_initializer())
        self.sess.run(tf.global_variables_initializer())
        print('[*] Variables initialized.')
        
        self.TrainSummary = tf.summary.merge_all()
        
        self._ensure_dirs('log')
        self.writer = tf.summary.FileWriter(self.log_dir, self.sess.graph)

        self.d_counter, self.g_counter, self.err_counter = 0, 0, 0
        
        if self.load(self.checkpoint_dir):
            print(""" [*] Load SUCCESS, re-starting at epoch %d with learning
                  rate %.7f""" % (self.sess.run(self.global_step), 
                                  self.sess.run(self.lr)))
        else:
            print(" [!] Load failed...")
#        self.sess.run(self.lr.assign(self.config.learning_rate))
        if (not self.config.MMD_lr_scheduler) and (self.sess.run(self.gp) == self.config.gradient_penalty):
            step = self.sess.run(self.global_step)
            lr_decays_so_far = int((step * 5.)/self.config.max_iteration)
            self.lr *= self.config.decay_rate ** lr_decays_so_far
            if 'decay_gp' in self.config.suffix:
                self.gp *= self.config.gp_decay_rate ** lr_decays_so_far
                print('current gradient penalty: %f' % self.sess.run(self.gp))
        print('current learning rate: %f' % self.sess.run(self.lr))    
        
        print('[*] Model initialized for training')

    def set_input_pipeline(self, streams=None):
        if self.dataset in ['mnist', 'cifar10']:
            path = os.path.join(self.data_dir, self.dataset)
            data_X, data_y = getattr(load, self.dataset)(path)
        elif self.dataset in ['celebA']:
            files = glob(os.path.join(self.data_dir, self.dataset, '*.jpg'))
            data_X = np.array([utils.get_image(f, 160, 160, resize_height=self.output_size, 
                                               resize_width=self.output_size) for f in files[:]])
        elif self.dataset == 'GaussianMix':
            G_config = {'g_line': None}
            path = os.path.join(self.sample_dir, self.description)
            data_X, G_config['ax1'], G_config['writer'] = load.GaussianMix(path)
            G_config['fig'] = G_config['ax1'].figure
            self.G_config = G_config
        else:
            raise ValueError("not implemented dataset '%s'" % self.dataset)
        if streams is None:
            streams = [self.real_batch_size]
        streams = np.cumsum(streams)
        bs = streams[-1]

        queue = tf.train.input_producer(tf.constant(data_X.astype(np.float32)), 
                                                shuffle=False)
        single_sample = queue.dequeue_many(bs * 4)
        single_sample.set_shape([bs * 4, self.output_size, self.output_size, self.c_dim])
        ims = tf.train.shuffle_batch(
            [single_sample], 
            batch_size=bs,
            capacity=max(bs * 8, self.batch_size * 32),
            min_after_dequeue=max(bs * 2, self.batch_size * 8),
            num_threads=4,
            enqueue_many=True
        )
        
        self.images = ims[:streams[0]]

        for j in np.arange(1, len(streams)):
            self.__dict__.update({'images%d' % (j + 1): ims[streams[j - 1]: streams[j]]})

        
    def set_input3_pipeline(self, streams=None):
        if streams is None:
            streams = [self.real_batch_size]
        streams = np.cumsum(streams)
        bs = streams[-1]
        read_batch = max(20000, bs * 10)

        self.files = glob(os.path.join(self.data_dir, self.dataset, '*.jpg'))
        self.read_count = 0
        def get_read_batch(k, limit=read_batch):
            with tf.device('/cpu:0'):
                rc = self.read_count
                self.read_count += read_batch
                if rc//len(self.files) < self.read_count//len(self.files):
                    self.files = list(np.random.permutation(self.files))
                tt = time.time()
                self.timer(rc, 'read start')
                ims = []
                files_k = self.files[k: k + read_batch] + self.files[: max(0, k + read_batch - len(self.files))]
                for ii, ff in enumerate(files_k):
                    ims.append(utils.get_image(ff, 160, 160, resize_height=self.output_size, 
                                               resize_width=self.output_size))
                self.timer(rc, 'read time = %f' % (time.time() - tt))
                return np.asarray(ims, dtype=np.float32)                
                

        choice = np.random.choice(len(self.files), 1)[0]
        sampled = get_read_batch(choice, self.sample_size + self.batch_size)

        # tf queue for getting keys
#        key_producer = tf.train.string_input_producer(keys, shuffle=True)
        key_producer = tf.train.range_input_producer(len(self.files), shuffle=True)
        single_key = key_producer.dequeue()
        
        single_sample = tf.py_func(get_read_batch, [single_key], tf.float32)
        single_sample.set_shape([read_batch, self.output_size, self.output_size, self.c_dim])

#        self.images = tf.train.shuffle_batch([single_sample], self.batch_size, 
#                                            capacity=read_batch * 4, 
#                                            min_after_dequeue=read_batch//2,
#                                            num_threads=2,
#                                            enqueue_many=True)
        ims = tf.train.shuffle_batch([single_sample], bs,
                                            capacity=read_batch * 16,
                                            min_after_dequeue=read_batch * 2,
                                            num_threads=8,
                                            enqueue_many=True)
        
        self.images = ims[:streams[0]]
        for j in np.arange(1, len(streams)):
            self.__dict__.update({'images%d' % (j + 1): ims[streams[j - 1]: streams[j]]})
 
    def set_jpeg_pipeline(self, streams=None):
        if streams is None:
            streams = [self.real_batch_size]
        streams = np.cumsum(streams)
        files = glob(os.path.join(self.data_dir, self.dataset, '*.jpg'))
        ims = utils.tf_read_jpeg(files, 
                               base_size=160, target_size=self.output_size, 
                               batch_size=streams[-1], 
                               capacity=4000, num_threads=4)
        self.images = ims[:streams[0]]
        for j in np.arange(1, len(streams)):
            self.__dict__.update({'images%d' % (j + 1): ims[streams[j - 1]: streams[j]]})

            
    def set_tf_records_pipeline(self, streams=None):     
        if streams is None:
            streams = [self.real_batch_size]
        streams = np.cumsum(streams)
        bs = streams[-1]
        
        path = '/nfs/data/dougals/'
        if not os.path.exists(path):
            path = self.config.data_dir
            
        with tf.device(tf.train.replica_device_setter(0, worker_device='/cpu:0')):
            filename_queue = tf.train.string_input_producer(
                tf.gfile.Glob(os.path.join(path, 'lsun-32/bedroom_train_*')), num_epochs=None)
            reader = tf.TFRecordReader()
            _, serialized_example = reader.read(filename_queue)
            features = tf.parse_single_example(serialized_example, features={
                'image/class/label': tf.FixedLenFeature([1], tf.int64),
                'image/encoded': tf.FixedLenFeature([], tf.string),
            })
            image = tf.image.decode_jpeg(features['image/encoded'])
            single_sample = tf.cast(image, tf.float32)/255.
            single_sample.set_shape([self.output_size, self.output_size, self.c_dim])

            ims = tf.train.shuffle_batch([single_sample], bs,
                                         capacity=bs * 8,
                                         min_after_dequeue=bs * 2, 
                                         num_threads=16,
                                         enqueue_many=False)
        self.images = ims[:streams[0]]
        for j in np.arange(1, len(streams)):
            self.__dict__.update({'images%d' % (j + 1): ims[streams[j - 1]: streams[j]]})
            
    def set_lmdb_pipeline(self, streams=None):
        if streams is None:
            streams = [self.real_batch_size]
        streams = np.cumsum(streams)
        bs = streams[-1]

        data_dir = os.path.join(self.data_dir, self.dataset)
        keys = []
        read_batch = max(4000, self.real_batch_size * 10)
        # getting keys in database
        env = lmdb.open(data_dir, map_size=1099511627776, max_readers=100, readonly=True)
        with env.begin() as txn:
            cursor = txn.cursor()
            while cursor.next():
                keys.append(cursor.key())
        print('Number of records in lmdb: %d' % len(keys))
        env.close()
        
        # value [np.array] reader for given key
        self.read_count = 0
        def get_sample_from_lmdb(key, limit=read_batch):
            with tf.device('/cpu:0'):
                rc = self.read_count
                self.read_count += 1
                tt = time.time()
                self.timer(rc, 'read start')
                env = lmdb.open(data_dir, map_size=1099511627776, max_readers=100, readonly=True)
                ims = []
                with env.begin(write=False) as txn:
                    cursor = txn.cursor()
                    cursor.set_key(key)
                    while len(ims) < limit:
                        key, byte_arr = cursor.item()
                        byte_im = io.BytesIO(byte_arr)
                        byte_im.seek(0)
                        try:
                            im = Image.open(byte_im)
                            ims.append(utils.center_and_scale(im, size=self.output_size))
                        except Exception as e:
                            print(e)
                        if not cursor.next():
                            cursor.first()
                env.close()
                self.timer(rc, 'read time = %f' % (time.time() - tt))
                return np.asarray(ims, dtype=np.float32)

        choice = np.random.choice(keys, 1)[0]
        sampled = get_sample_from_lmdb(choice, self.sample_size + self.batch_size)

        # tf queue for getting keys
        key_producer = tf.train.string_input_producer(keys, shuffle=True)
        single_key = key_producer.dequeue()
        
        single_sample = tf.py_func(get_sample_from_lmdb, [single_key], tf.float32)
        single_sample.set_shape([read_batch, self.output_size, self.output_size, self.c_dim])

#        self.images = tf.train.shuffle_batch([single_sample], self.batch_size, 
#                                            capacity=read_batch * 4, 
#                                            min_after_dequeue=read_batch//2,
#                                            num_threads=2,
#                                            enqueue_many=True)
        ims = tf.train.shuffle_batch([single_sample], bs,
                                            capacity=max(bs * 8, read_batch),
                                            min_after_dequeue=max(bs * 2, read_batch//8),
                                            num_threads=16,
                                            enqueue_many=True)
        
        self.images = ims[:streams[0]]
        for j in np.arange(1, len(streams)):
            self.__dict__.update({'images%d' % (j + 1): ims[streams[j - 1]: streams[j]]})
        
            
            
    def set_pipeline(self):
        Pipeline = pipeline.get_pipeline(self.dataset, self.config.suffix)
        pipe = Pipeline(self.output_size, self.c_dim, self.real_batch_size, 
                        os.path.join(self.data_dir, self.dataset), 
                        timer=self.timer, sample_dir=self.sample_dir)
        self.images = pipe.connect()        

            
    def train(self):    
        self.train_init()

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=self.sess, coord=coord)        
        step = 0
        
        print('[ ] Training ... ')
        while step <= self.config.max_iteration:
            g_loss, d_loss, step = self.train_step()
            self.save_samples(step)
            if self.config.save_layer_outputs:
                self.save_layers(step)
            if self.dataset == 'GaussianMix':
                self.make_video(step, self.pipe.G_config, g_loss)
        if self.dataset == 'GaussianMix':
            self.pipe.G_config['writer'].finish()   
            
        coord.request_stop()
        coord.join(threads)
        

    def sampling(self, config):
        self.sess.run(tf.local_variables_initializer())
        self.sess.run(tf.global_variables_initializer())
        print(self.checkpoint_dir)
        if self.load(self.checkpoint_dir):
            print("sucess")
        else:
            print("fail")
            return
        n = 1000
        batches = n // self.batch_size
        sample_dir = os.path.join("official_samples", config.name)
        if not os.path.exists(sample_dir):
            os.makedirs(sample_dir)
        for batch_id in range(batches):
            [G] = self.sess.run([self.G])
            print("G shape", G.shape)
            for i in range(self.batch_size):
                G_tmp = np.zeros((28, 28, 3))
                G_tmp[:,:,:1] = G[i]
                G_tmp[:,:,1:2] = G[i]
                G_tmp[:,:,2:3] = G[i]

                n = i + batch_id * self.batch_size
                p = os.path.join(sample_dir, "img_{}.png".format(n))
                scipy.misc.imsave(p, G_tmp)


    def save(self, checkpoint_dir, step):
        self._ensure_dirs('checkpoint')
        if step is None:
            self.saver.save(self.sess,
                            os.path.join(self.checkpoint_dir, "best.model"))
        else:
            self.saver.save(self.sess,
                            os.path.join(self.checkpoint_dir, "MMDGAN.model"),
                            global_step=step)


    def load(self, checkpoint_dir):
        print(" [*] Reading checkpoints...")
        ckpt = tf.train.get_checkpoint_state(self.checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver.restore(self.sess, os.path.join(self.checkpoint_dir, ckpt_name))
            return True
        else:
            return False
        
        
    def imageRearrange(self, image, block=4):
        image = tf.slice(image, [0, 0, 0, 0], [block * block, -1, -1, -1])
        x1 = tf.batch_to_space(image, [[0, 0], [0, 0]], block)
        image_r = tf.reshape(tf.transpose(tf.reshape(x1,
            [self.output_size, block, self.output_size, block, self.c_dim])
            , [1, 0, 3, 2, 4]),
            [1, self.output_size * block, self.output_size * block, self.c_dim])
        return image_r


    def save_samples(self, step, freq=1000):
        if (np.mod(step, freq) == 0) and (self.d_counter == 0):
            self.save(self.checkpoint_dir, step)
            samples = self.sess.run(self.sampler)
            self._ensure_dirs('sample')
            p = os.path.join(self.sample_dir, 'train_{:02d}.png'.format(step))
            utils.save_images(samples[:64, :, :, :], [8, 8], p)  
            
            
    def save_layers(self, step, freq=1000, n=256, layers=[-1, -2]):
        c = self.config.save_layer_outputs
        valid = list(freq * np.arange(self.config.max_iteration/freq + 1))
        if c > 1:
            valid += [int(k) for k in c**np.arange(np.log(freq)/np.log(c))]
        if (step in valid) and (self.d_counter == 0):
            if not (layers == 'all'):
                keys = [sorted(list(self.d_G_layers))[i] for i in layers]
            fake = [(key + '_fake', self.d_G_layers[key]) for key in keys] 
            real = [(key + '_real', self.d_images_layers[key]) for key in keys]
            
            values = self._evaluate(dict(real + fake), n=n)    
            path = os.path.join(self.sample_dir, 'layer_outputs_%d.npz' % step)
            np.savez(path, **values)
            
    def _evaluate(self, variable_dict, n=None):
        if n is None:
            n = self.batch_size
        values = dict([(key, []) for key in variable_dict.keys()])
        sampled = 0
        while sampled < n:
            vv = self.sess.run(variable_dict)
            for key, val in vv.items():
                values[key].append(val)
            sampled += list(vv.items())[0][1].shape[0]
        for key, val in values.items():
            values[key] = np.concatenate(val, axis=0)[:n]        
        return values
        
    def get_samples(self, n=None, save=True, layers=[]):
        if not (self.initialized_for_sampling or self.config.is_train):
            print('[*] Loading from ' + self.checkpoint_dir + '...')
            self.sess.run(tf.local_variables_initializer())
            self.sess.run(tf.global_variables_initializer())
            if self.load(self.checkpoint_dir):
                print(" [*] Load SUCCESS, model trained up to epoch %d" % \
                      self.sess.run(self.global_step))
            else:
                print(" [!] Load failed...")
                return
    
        if len(layers) > 0:
            outputs = dict([(key + '_features', val) for key, val in self.d_G_layers.items()])
            if not (layers == 'all'):
                keys = [sorted(list(outputs.keys()))[i] for i in layers]
                outputs = dict([(key, outputs[key]) for key in keys])
        else:
            outputs = {}
        outputs['samples'] = self.G

        values = self._evaluate(outputs, n=n)
        
        if not save:
            if len(layers) > 0:
                return values
            return values['samples']
        
        for key, val in values.items():
            file = os.path.join(self.sample_dir, '%s.npy' % key)
            np.save(file, val, allow_pickle=False)
            print(" [*] %d %s saved in '%s'" % (n, key, file))          
    
    
    def print_pca(self, n=10000, return_=False, layers=[-1, -2]):
        from sklearn.decomposition import PCA
        features = self.get_samples(n=n, save=False, layers=layers)
        del features['samples']
        pcas = {}
        if not return_:
            print('%s %s critic size: %d filters, %d top layer, batch size: %d' % \
                  (self.config.model, self.config.kernel, self.df_dim, self.dof_dim, self.batch_size))
        for key, val in features.items():
            feats = val.size//val.shape[0]
            pca = PCA()
            pca.fit(val.reshape((val.shape[0], feats)))
            if return_:
                pcas[key] = pca.explained_variance_ratio_
            main_pcs = pca.explained_variance_ratio_[:5]
            summary = ' '.join(['%.3f' % ev for ev in main_pcs])
            print('%13s: %30s (%.1f%% variance in %d/%d PCs)' % (key, summary, \
                  100.* main_pcs.sum(), len(main_pcs), feats))
                    
                    
    def make_video(self, step, G_config, optim_loss, freq=10):
        if np.mod(step, freq) == 1:          
            samples = self.sess.run(self.sampler)
            if G_config['g_line'] is not None:
                G_config['g_line'].remove()
            G_config['g_line'], = load.myhist(samples, ax=G_config['ax1'], color='b')
            plt.title("Iteration {: 6}:, loss {:7.4f}".format(
                    step, optim_loss))
            G_config['writer'].grab_frame()
            if step % 100 == 0:
                display(G_config['fig'])