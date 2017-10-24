from __future__ import division, print_function
from glob import glob
import os
import time

import numpy as np
import scipy.misc
from six.moves import xrange
import tensorflow as tf
import matplotlib.pyplot as plt
from PIL import Image
import lmdb
import io
import sys
from IPython.display import display

import mmd as MMD
import load
from ops import batch_norm, conv2d, deconv2d, linear, lrelu
from utils import *
import pprint
from mmd import _eps, _check_numerics

class MMD_GAN(object):
    def __init__(self, sess, config, is_crop=True,
                 batch_size=64, output_size=64,
                 z_dim=100,
                 gfc_dim=1024, dfc_dim=1024, c_dim=3, dataset_name='default',
                 checkpoint_dir=None, sample_dir=None, log_dir=None, 
                 data_dir=None, gradient_clip=1.0):
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
        self.start_time = time.time()
        self.check_numerics = _check_numerics
        self.sess = sess
        if config.architecture == 'dc128':
            output_size = 128
        elif config.output_size == 128:
            config.architecture = 'dc128'
        if config.architecture in ['dc64', 'dcgan64']:
            output_size = 64
#        elif config.output_size == 64:
#            config.architecture = 'dc64'
        if config.real_batch_size == -1:
            config.real_batch_size = config.batch_size
        self.config = config
        self.is_crop = is_crop
        self.is_grayscale = (c_dim == 1)
        self.batch_size = batch_size
        self.real_batch_size = config.real_batch_size
        self.sample_size = batch_size
#        if self.config.dataset == 'GaussianMix':
#            self.sample_size = min(16 * batch_size, 512)
        if self.config.gradient_penalty > 0:
            config.suffix = '_' + config.gp_type + config.suffix
        self.output_size = output_size
        self.sample_dir = sample_dir + config.suffix
        self.log_dir=log_dir + config.suffix
        self.checkpoint_dir = checkpoint_dir + config.suffix
        self.data_dir = data_dir
        self.z_dim = z_dim

        self.gf_dim = config.gf_dim
        self.df_dim = config.df_dim
        self.dof_dim = self.config.dof_dim

        self.gfc_dim = gfc_dim
        self.dfc_dim = dfc_dim

        self.c_dim = c_dim

        # G batch normalization : deals with poor initialization helps gradient flow
        if self.config.batch_norm:
            self.g_bn0 = batch_norm(name='g_bn0')
            self.g_bn1 = batch_norm(name='g_bn1')
            self.g_bn2 = batch_norm(name='g_bn2')
            self.g_bn3 = batch_norm(name='g_bn3')
            self.g_bn4 = batch_norm(name='g_bn4')
            self.g_bn5 = batch_norm(name='g_bn5')
        else:
            self.g_bn0 = lambda x: x
            self.g_bn1 = lambda x: x
            self.g_bn2 = lambda x: x
            self.g_bn3 = lambda x: x
            self.g_bn4 = lambda x: x
            self.g_bn5 = lambda x: x
        # D batch normalization
        if self.config.batch_norm and (self.config.gradient_penalty <= 0):
            self.d_bn0 = batch_norm(name='d_bn0')
            self.d_bn1 = batch_norm(name='d_bn1')
            self.d_bn2 = batch_norm(name='d_bn2')
            self.d_bn3 = batch_norm(name='d_bn3')
            self.d_bn4 = batch_norm(name='d_bn4')
            self.d_bn5 = batch_norm(name='d_bn5')
        else:
            self.d_bn0 = lambda x: x
            self.d_bn1 = lambda x: x
            self.d_bn2 = lambda x: x
            self.d_bn3 = lambda x: x
            self.d_bn4 = lambda x: x
            self.d_bn5 = lambda x: x
            
        self.dataset_name = dataset_name
        
        discriminator_desc = '_dc' if self.config.dc_discriminator else ''
        d_clip = self.config.discriminator_weight_clip
        dwc = ('_dwc_%f' % d_clip) if (d_clip > 0) else ''
        if self.config.learning_rate_D == self.config.learning_rate:
            lr = 'lr%.8f' % self.config.learning_rate
        else:
            lr = 'lr%.8fG%fD' % (self.config.learning_rate, self.config.learning_rate_D)
        arch = '%dx%d' % (self.config.gf_dim, self.config.df_dim)

        self.description = ("%s%s_%s%s_%s%sd%d-%d-%d_%s_%s_%s" % (
                    self.dataset_name, arch,
                    self.config.architecture, discriminator_desc,
                    self.config.kernel, dwc, self.config.dsteps,
                    self.config.start_dsteps, self.config.gsteps, self.batch_size,
                    self.output_size, lr))
        if self.config.batch_norm:
            self.description += '_bn'
            
        if self.config.log:
            sample_dir = os.path.join(self.sample_dir, self.description)
            if not os.path.exists(sample_dir):
                os.makedirs(sample_dir)
            self.old_stdout = sys.stdout
            self.old_stderr = sys.stderr
            self.log_file = open(os.path.join(sample_dir, 'log.txt'), 'w', buffering=1)
            print('Execution start time: %s' % time.ctime())
            print('Log file: %s' % self.log_file)
            sys.stdout = self.log_file
            sys.stderr = self.log_file
        print('Execution start time: %s' % time.ctime())
        pprint.PrettyPrinter().pprint(self.config.__dict__['__flags'])
        self.build_model()
        
        if not config.is_train:
            self.initialized_for_sampling = False
            

    def build_model(self):
        self.global_step = tf.Variable(0, name="global_step", trainable=False)
        self.lr = tf.get_variable('lr', dtype=tf.float32, initializer=self.config.learning_rate)
        if 'lsun' in self.config.dataset:
            self.set_lmdb_pipeline()
        elif self.config.dataset == 'celebA':
            self.set_input3_pipeline()
        else:
            self.set_input_pipeline()

        self.z = tf.random_uniform([self.batch_size, self.z_dim], minval=-1., 
                                   maxval=1., dtype=tf.float32, name='z')
        self.sample_z = tf.constant(np.random.uniform(-1, 1, size=(self.sample_size, 
                                                      self.z_dim)).astype(np.float32),
                                    dtype=tf.float32, name='sample_z')        

        # tf.summary.histogram("z", self.z)
        if not self.config.single_batch_experiment:
            self.G = self.generator(self.z)
        else:
            self.G = self.generator(self.sample_z)
            self.images = tf.constant(self.additional_sample_images, dtype=tf.float32, name='im')
        
        if self.check_numerics:
            self.G = tf.check_numerics(self.G, 'self.G')
        self.sampler = self.generator(self.sample_z, is_train=False, reuse=True)
        
        if self.config.dc_discriminator:
            images = self.discriminator(self.images, reuse=False, batch_size=self.real_batch_size)
            G = self.discriminator(self.G, reuse=True)
        else:
            images = tf.reshape(self.images, [self.real_batch_size, -1])
            G = tf.reshape(self.G, [self.batch_size, -1])

        self.set_loss(G, images)

        block = min(8, int(np.sqrt(self.real_batch_size)), int(np.sqrt(self.batch_size)))
        tf.summary.image("train/input image", 
                         self.imageRearrange(tf.clip_by_value(self.images, 0, 1), block))
        tf.summary.image("train/gen image", 
                         self.imageRearrange(tf.clip_by_value(self.G, 0, 1), block))
        
        t_vars = tf.trainable_variables()

        self.d_vars = [var for var in t_vars if 'd_' in var.name]
        self.g_vars = [var for var in t_vars if 'g_' in var.name]

        self.saver = tf.train.Saver(max_to_keep=2)

    def set_loss(self, G, images):
        if self.check_numerics:
            G = tf.check_numerics(G, 'G')
            images = tf.check_numerics(images, 'images')
        if self.config.kernel == 'di': # Distance - induced kernel
            self.di_kernel_z_images = tf.constant(
                self.additional_sample_images,
                dtype=tf.float32,                                  
                name='di_kernel_z_images'
            )
            alphas = [1.0]
            di_r = np.random.choice(np.arange(self.batch_size))
            if self.config.dc_discriminator:
                self.di_kernel_z = self.discriminator(
                        self.di_kernel_z_images, reuse=True)[di_r: di_r + 1]
            else:
                self.di_kernel_z = tf.reshape(self.di_kernel_z_images[di_r: di_r + 1], [1, -1])
            kernel = lambda gg, ii, K_XY_only=False: MMD._mix_di_kernel(
                    gg, ii, self.di_kernel_z, alphas=alphas, K_XY_only=K_XY_only)
        else:
            kernel = getattr(MMD, '_%s_kernel' % self.config.kernel)
            
        kerGI = kernel(G, images)
        with tf.variable_scope('loss'):
            if self.config.model in ['mmd', 'mmd_gan']:
                self.mmd_loss = MMD.mmd2(kerGI)
                self.optim_name = 'kernel_loss'
            elif self.config.model == 'tmmd':
                kl, rl, var_est = MMD.mmd2_and_ratio(kerGI)
                self.mmd_loss = rl
                self.optim_name = 'ratio_loss'
                tf.summary.scalar("kernel_loss", kl)#tf.sqrt(kl + _eps))
                tf.summary.scalar("variance_estimate", var_est)
            
        self.add_gradient_penalty(kernel, G, images)
        

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
            alpha = .99 + .01 * alpha

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
            x_hat = self.discriminator(x_hat_data, reuse=True)
            if self.check_numerics:
                x_hat = tf.check_numerics(x_hat, 'x_hat')
            Ekx = lambda yy: tf.reduce_mean(kernel(x_hat, yy, K_XY_only=True), axis=1)
            witness = Ekx(real) - Ekx(fake)
            if self.check_numerics:
                witness = tf.check_numerics(witness, 'witness')
            gradients = tf.gradients(witness, [x_hat_data])[0]
            if self.check_numerics:
                gradients = tf.check_numerics(gradients, 'gradients 0')
        elif self.config.gp_type == 'wgan':
            alpha = tf.reshape(alpha, [bs, 1, 1, 1])
            real_data = self.images #before discirminator
            fake_data = self.G #before discriminator
            x_hat_data = (1. - alpha) * real_data + alpha * fake_data
            x_hat = self.discriminator(x_hat_data, reuse=True)
            gradients = tf.gradients(x_hat, [x_hat_data])[0]
        
        if self.check_numerics:  
            penalty = tf.check_numerics(tf.reduce_mean(tf.square(tf.norm(gradients, axis=1) - 1.0)), 'penalty')
        else:
            penalty = tf.reduce_mean(tf.square(tf.norm(gradients, axis=1) - 1.0))#

        
        print('adding gradient penalty')
        with tf.variable_scope('loss'):
            if self.config.gradient_penalty > 0:
                self.gp = tf.get_variable('gradient_penalty', dtype=tf.float32,
                                          initializer=self.config.gradient_penalty)
                self.g_loss = self.mmd_loss
                self.d_loss = -self.mmd_loss + penalty * self.gp
                self.optim_name += ' gp %.1f' % self.config.gradient_penalty
            else:
                self.g_loss = self.mmd_loss
                self.d_loss = -self.mmd_loss
            tf.summary.scalar(self.optim_name + ' G', self.g_loss)
            tf.summary.scalar(self.optim_name + ' D', self.d_loss)
            tf.summary.scalar('dx_penalty', penalty)
        
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
    
    def train_step(self, batch_images=None):
        step = self.sess.run(self.global_step)
        write_summary = ((np.mod(step, 50) == 0) and (step < 1000)) \
                or (np.mod(step, 1000) == 0) or (self.err_counter > 0)
        # write_summary=True
        if self.config.use_kernel:
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
                et = "g step" if (self.d_counter == 0) else "d step"
                et += ", epoch: [%2d] time: %4.4f" % (step, time.time() - self.start_time)

        # G STEP
        if self.d_counter == 0:
            if step % 10000 == 0:
                try:
                    self.writer.add_summary(summary_str, step)
                    self.err_counter = 0
                except Exception as e:
                    print('Step %d summary exception. ' % step, e)
                    self.err_counter += 1
            if write_summary:
                print("Epoch: [%2d] time: %4.4f, %s, G: %.8f, D: %.8f"
                    % (step, time.time() - self.start_time, 
                       self.optim_name, g_loss, d_loss))
            if np.mod(step + 1, self.config.max_iteration//5) == 0:
                self.lr *= self.config.decay_rate
                print('current learning rate: %f' % self.sess.run(self.lr))
                if ('decay_gp' in self.config.suffix) and (self.config.gradient_penalty > 0):
                    self.gp *= self.config.decay_rate
                    print('current gradient penalty: %f' % self.sess.run(self.gp))
            
        if (step == 1) & (self.d_counter == 0):
            print('current learning rate: %f' % self.sess.run(self.lr))
        if (self.g_counter == 0) and (self.d_grads is not None):
            d_steps = self.config.dsteps
            if ((step % 100 == 0) or (step < 20)):
                d_steps = self.config.start_dsteps
            self.d_counter = (self.d_counter + 1) % (d_steps + 1)
        if self.d_counter == 0:
            self.g_counter = (self.g_counter + 1) % self.config.gsteps
        return g_loss, d_loss, step
      

    def train_init(self):
        if self.config.use_kernel:
            self.set_grads()

        self.sess.run(tf.global_variables_initializer())
        self.TrainSummary = tf.summary.merge_all()
        
        log_dir = os.path.join(self.log_dir, self.description)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        self.writer = tf.summary.FileWriter(log_dir, self.sess.graph)

        self.d_counter, self.g_counter, self.err_counter = 0, 0, 0
        
        if self.load(self.checkpoint_dir):
            print(" [*] Load SUCCESS, re-starting at epoch %d" % self.sess.run(self.global_step))
        else:
            print(" [!] Load failed...")

    def set_input_pipeline(self, streams=None):
        if self.config.dataset in ['mnist', 'cifar10']:
            path = os.path.join(self.data_dir, self.dataset_name)
            data_X, data_y = getattr(load, self.config.dataset)(path)
        elif self.config.dataset in ['celebA']:
            files = glob(os.path.join(self.data_dir, self.dataset_name, '*.jpg'))
            data_X = np.array([get_image(f, 144, 144, resize_height=self.output_size, 
                                         resize_width=self.output_size) for f in files[:]])
        elif self.config.dataset == 'GaussianMix':
            G_config = {'g_line': None}
            path = os.path.join(self.sample_dir, self.description)
            data_X, G_config['ax1'], G_config['writer'] = load.GaussianMix(path)
            G_config['fig'] = G_config['ax1'].figure
            self.G_config = G_config
        else:
            raise ValueError("not implemented dataset '%s'" % self.config.dataset)
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
        off = int(np.random.rand()*( data_X.shape[0] - self.batch_size*2))
        self.additional_sample_images = data_X[off: off + self.batch_size].astype(np.float32)

        
    def set_input3_pipeline(self, streams=None):
        if streams is None:
            streams = [self.real_batch_size]
        streams = np.cumsum(streams)
        bs = streams[-1]
        read_batch = max(20000, bs * 10)

        self.files = glob(os.path.join(self.data_dir, self.dataset_name, '*.jpg'))
        self.read_count = 0
        def get_read_batch(k, limit=read_batch):
            with tf.device('/cpu:0'):
                rc = self.read_count
                self.read_count += read_batch
                if rc//len(self.files) < self.read_count//len(self.files):
                    self.files = list(np.random.permutation(self.files))
                tt = time.time()
                print('[%d][%f] read start' % (rc, tt - self.start_time))
                ims = []
                files_k = self.files[k: k + read_batch] + self.files[: max(0, k + read_batch - len(self.files))]
                for ii, ff in enumerate(files_k):
                    ims.append(get_image(ff, 144, 144, resize_height=self.output_size, 
                                                  resize_width=self.output_size))
                print('[%d][%f] read time = %f' % (rc, time.time() - self.start_time, time.time() - tt))
                return np.asarray(ims, dtype=np.float32)                
                

        choice = np.random.choice(len(self.files), 1)[0]
        sampled = get_read_batch(choice, self.sample_size + self.batch_size)

        self.additional_sample_images = sampled[self.sample_size: self.sample_size + self.batch_size]
        print('self.additional_sample_images.shape: ' + repr(self.additional_sample_images.shape))
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
            
            
    def set_lmdb_pipeline(self, streams=None):
        if streams is None:
            streams = [self.real_batch_size]
        streams = np.cumsum(streams)
        bs = streams[-1]

        data_dir = os.path.join(self.data_dir, self.dataset_name)
        keys = []
        read_batch = max(10000, self.real_batch_size * 20)
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
                print('[%d][%f] read start' % (rc, tt - self.start_time))
                env = lmdb.open(data_dir, map_size=1099511627776, max_readers=100, readonly=True)
                ims = []
                with env.begin(write=False) as txn:
                    cursor = txn.cursor()
                    cursor.set_key(key)
                    while len(ims) < limit:
                        key, byte_arr = cursor.item()
                        im = Image.open(io.BytesIO(byte_arr))
                        ims.append(center_and_scale(im, size=self.output_size))
                        if not cursor.next():
                            cursor.first()
                env.close()
                print('[%d][%f] read time = %f' % (rc, time.time() - self.start_time, time.time() - tt))
                return np.asarray(ims, dtype=np.float32)

        choice = np.random.choice(keys, 1)[0]
        sampled = get_sample_from_lmdb(choice, self.sample_size + self.batch_size)

        self.additional_sample_images = sampled[self.sample_size: self.sample_size + self.batch_size]
        print('self.additional_sample_images.shape: ' + repr(self.additional_sample_images.shape))
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
                                            capacity=max(bs * 8, read_batch * 4),
                                            min_after_dequeue=max(bs * 2, read_batch//2),
                                            num_threads=16,
                                            enqueue_many=True)
        
        self.images = ims[:streams[0]]
        for j in np.arange(1, len(streams)):
            self.__dict__.update({'images%d' % (j + 1): ims[streams[j - 1]: streams[j]]})
        
    def discriminator(self, image, y=None, reuse=False, batch_size=None):
        if batch_size is None:
            batch_size = self.batch_size
        with tf.variable_scope("discriminator") as scope:
            if reuse:
                scope.reuse_variables()
            if 'dfc' in self.config.architecture:
                h0 = lrelu(conv2d(image, self.df_dim, k_h=4, k_w=4, name='d_h0_conv'))
                h1 = lrelu(self.d_bn1(conv2d(h0, self.df_dim * 2, k_h=4, k_w=4, name='d_h1_conv')))
                h2 = lrelu(self.d_bn2(conv2d(h1, self.df_dim * 4, k_h=4, k_w=4, name='d_h2_conv')))
                h3 = conv2d(h2, self.df_dim, d_h=4, d_w=4, k_h=4, k_w=4, name='d_h3_conv')
                hF = tf.reshape(h3, [batch_size, self.df_dim])
                self.dof_dim = self.df_dim
            elif 'dcold' in self.config.architecture:
                h0 = lrelu(conv2d(image, self.df_dim//8, name='d_h0_conv'))
                h1 = lrelu(self.d_bn1(conv2d(h0, self.df_dim//4, name='d_h1_conv')))
                h2 = lrelu(self.d_bn2(conv2d(h1, self.df_dim//2, name='d_h2_conv')))
                h3 = lrelu(self.d_bn3(conv2d(h2, self.df_dim, name='d_h3_conv')))
                hF = linear(tf.reshape(h3, [batch_size, -1]), self.df_dim, 'd_h4_lin')
                self.dof_dim = self.df_dim
            elif 'dc64' in self.config.architecture:
                if self.dof_dim <= 0:
                    self.dof_dim = self.df_dim * 16
                h0 = lrelu(conv2d(image, self.df_dim, name='d_h0_conv'))
                h1 = lrelu(self.d_bn1(conv2d(h0, self.df_dim * 2, name='d_h1_conv')))
                h2 = lrelu(self.d_bn2(conv2d(h1, self.df_dim * 4, name='d_h2_conv')))
                h3 = lrelu(self.d_bn3(conv2d(h2, self.df_dim * 8, name='d_h3_conv')))
                h4 = lrelu(self.d_bn4(conv2d(h3, self.df_dim * 16, name='d_h4_conv')))
                hF = linear(tf.reshape(h4, [batch_size, -1]), self.dof_dim, 'd_h6_lin')
            elif 'dc128' in self.config.architecture:
                if self.dof_dim <= 0:
                    self.dof_dim = self.df_dim * 32
                h0 = lrelu(conv2d(image, self.df_dim, name='d_h0_conv'))
                h1 = lrelu(self.d_bn1(conv2d(h0, self.df_dim * 2, name='d_h1_conv')))
                h2 = lrelu(self.d_bn2(conv2d(h1, self.df_dim * 4, name='d_h2_conv')))
                h3 = lrelu(self.d_bn3(conv2d(h2, self.df_dim * 8, name='d_h3_conv')))
                h4 = lrelu(self.d_bn4(conv2d(h3, self.df_dim * 16, name='d_h4_conv')))
                h5 = lrelu(self.d_bn5(conv2d(h4, self.df_dim * 32, name='d_h5_conv')))
                hF = linear(tf.reshape(h5, [batch_size, -1]), self.dof_dim , 'd_h6_lin')
            elif 'dcgan' in self.config.architecture:
                if self.dof_dim <= 0:
                    self.dof_dim = self.df_dim * 8
                h0 = lrelu(conv2d(image, self.df_dim, name='d_h0_conv'))
                h1 = lrelu(self.d_bn1(conv2d(h0, self.df_dim * 2, name='d_h1_conv')))
                h2 = lrelu(self.d_bn2(conv2d(h1, self.df_dim * 4, name='d_h2_conv')))
                h3 = lrelu(self.d_bn3(conv2d(h2, self.df_dim * 8, name='d_h3_conv')))
                hF = linear(tf.reshape(h3, [batch_size, -1]), self.dof_dim, 'd_h4_lin')
            else:
                raise ValueError("Choose architecture from  [dfc, dcold, dcgan, dc64, dc128]")
            print(repr(image.get_shape()).replace('Dimension', '') + ' --> Discriminator --> ' + \
                  repr(hF.get_shape()).replace('Dimension', ''))
            return hF

        
    def generator(self, z, y=None, is_train=True, reuse=False, batch_size=None):
        if batch_size is None:
            batch_size = self.batch_size
        if self.config.dataset not in ['mnist', 'cifar10', 'lsun', 'GaussianMix', 'celebA']:
            raise ValueError("not implemented dataset '%s'" % self.config.dataset)
        elif self.config.dataset in ['lsun', 'cifar10']:
            if self.config.architecture == 'mlp':
                return self.MLP_generator(z, is_train=is_train, reuse=reuse)
        with tf.variable_scope('generator') as scope:
            if reuse:
                scope.reuse_variables()
            s1, s2, s4, s8, s16 = conv_sizes(self.output_size, layers=4, stride=2)
            if 'dfc' in self.config.architecture:
                z_ = tf.reshape(z, [batch_size, 1, 1, -1])
                h0 = tf.nn.relu(self.g_bn0(deconv2d(z_, 
                    [batch_size, s8, s8, self.gf_dim * 4], name='g_h0_conv',
                    k_h=4, k_w=4, d_h=4, d_w=4)))
                h1 = tf.nn.relu(self.g_bn1(deconv2d(h0, 
                    [batch_size, s4, s4, self.gf_dim * 2], name='g_h1_conv', k_h=4, k_w=4)))
                h2 = tf.nn.relu(self.g_bn2(deconv2d(h1, 
                    [batch_size, s2, s2, self.gf_dim], name='g_h2_conv', k_h=4, k_w=4)))
                h3 = deconv2d(h2, [batch_size, s1, s1, self.c_dim], name='g_h3_conv', k_h=4, k_w=4)
                return tf.nn.sigmoid(h3)
            elif 'dcold' in self.config.architecture:
                # project `z` and reshape
                self.z_, self.h0_w, self.h0_b = linear(
                    z, self.gf_dim*8*s16*s16, 'g_h0_lin', with_w=True)

                h0 = tf.nn.relu(self.g_bn0(self.z_))
                
                h1, self.h1_w, self.h1_b = linear(
                        h0, self.gf_dim*4*s8*s8, 'g_h1_lin', with_w=True)
                h1 = tf.nn.relu(self.g_bn1(tf.reshape(h1, [-1, s8, s8, self.gf_dim*4])))
                
                h2, self.h2_w, self.h2_b = deconv2d(
                    h1, [batch_size, s4, s4, self.gf_dim*2], name='g_h2', with_w=True)
                h2 = tf.nn.relu(self.g_bn2(h2))
                
                h3, self.h3_w, self.h3_b = deconv2d(
                    h2, [batch_size, s2, s2, self.gf_dim*1], name='g_h3', with_w=True)
                h3 = tf.nn.relu(self.g_bn3(h3))
                
                h4, self.h4_w, self.h4_b = deconv2d(
                    h3, [batch_size, s1, s1, self.c_dim], name='g_h4', with_w=True)
                return tf.nn.sigmoid(h4)
            elif 'dc64' in self.config.architecture:
                s1, s2, s4, s8, s16, s32 = conv_sizes(self.output_size, layers=5, stride=2)
                # project `z` and reshape
                z_= linear(z, self.gf_dim*16*s32*s32, 'g_h0_lin')
                
                h0 = tf.reshape(z_, [-1, s32, s32, self.gf_dim * 16])
                h0 = tf.nn.relu(self.g_bn0(h0))
                
                h1 = deconv2d(h0, [batch_size, s16, s16, self.gf_dim*8], name='g_h1')
                h1 = tf.nn.relu(self.g_bn1(h1))
                                
                h2 = deconv2d(h1, [batch_size, s8, s8, self.gf_dim*4], name='g_h2')
                h2 = tf.nn.relu(self.g_bn2(h2))

                h3 = deconv2d(h2, [batch_size, s4, s4, self.gf_dim*2], name='g_h3')
                h3 = tf.nn.relu(self.g_bn3(h3))

                h4 = deconv2d(h3, [batch_size, s2, s2, self.gf_dim], name='g_h4')
                h4 = tf.nn.relu(self.g_bn4(h4))                
                
                h5 = deconv2d(h4, [batch_size, s1, s1, self.c_dim], name='g_h5')
                return tf.nn.sigmoid(h5)
            elif 'dc128' in self.config.architecture:
                s1, s2, s4, s8, s16, s32, s64 = conv_sizes(self.output_size, layers=6, stride=2)
                # project `z` and reshape
                z_= linear(z, self.gf_dim*32*s64*s64, 'g_h0_lin')

                h0 = tf.reshape(z_, [-1, s64, s64, self.gf_dim * 32])
                h0 = tf.nn.relu(self.g_bn0(h0))

                h1 = deconv2d(h0, [batch_size, s32, s32, self.gf_dim*16], name='g_h1')
                h1 = tf.nn.relu(self.g_bn1(h1))

                h2 = deconv2d(h1, [batch_size, s16, s16, self.gf_dim*8], name='g_h2')
                h2 = tf.nn.relu(self.g_bn2(h2))

                h3 = deconv2d(h2, [batch_size, s8, s8, self.gf_dim*4], name='g_h3')
                h3 = tf.nn.relu(self.g_bn3(h3))

                h4 = deconv2d(h3, [batch_size, s4, s4, self.gf_dim*2], name='g_h4')
                h4 = tf.nn.relu(self.g_bn4(h4))

                h5 = deconv2d(h4, [batch_size, s2, s2, self.gf_dim*1], name='g_h5')
                h5 = tf.nn.relu(self.g_bn5(h5))

                h6 = deconv2d(h5, [batch_size, s1, s1, self.c_dim], name='g_h6')
                # with tf.name_scope('G_outputs'):
                    # variable_summaries([(h0, 'h0'), (h1, 'h1'), (h2, 'h2'),
                    #                     (h3, 'h3'), (h4, 'h4'), (h5, 'h5'),
                    #                     (h6, 'h6')])
                return tf.nn.sigmoid(h6)
            elif 'dcgan' in self.config.architecture:
                # project `z` and reshape
                z_ = linear(z, self.gf_dim*8*s16*s16, 'g_h0_lin')
                
                h0 = tf.reshape(z_, [batch_size, s16, s16, self.gf_dim * 8])
                h0 = tf.nn.relu(self.g_bn0(h0))
                
                h1 = deconv2d(h0, [batch_size, s8, s8, self.gf_dim*4], name='g_h1')
                h1 = tf.nn.relu(self.g_bn1(h1))
                                
                h2 = deconv2d(h1, [batch_size, s4, s4, self.gf_dim*2], name='g_h2')
                h2 = tf.nn.relu(self.g_bn2(h2))
                
                h3 = deconv2d(h2, [batch_size, s2, s2, self.gf_dim*1], name='g_h3')
                h3 = tf.nn.relu(self.g_bn3(h3))
                
                h4 = deconv2d(h3, [batch_size, s1, s1, self.c_dim], name='g_h4')
                # with tf.name_scope('G_outputs'):
                    # variable_summaries([(h0, 'h0'), (h1, 'h1'), (h2, 'h2'),
                    #                     (h3, 'h3'),
                    #                     (h4, 'h4')])
                return tf.nn.sigmoid(h4)
            
    def train(self):    
        self.train_init()

        coord = tf.train.Coordinator()
        threads = tf.train.start_queue_runners(sess=self.sess, coord=coord)        
        step = 0
        while step < self.config.max_iteration:
            g_loss, d_loss, step = self.train_step()
            self.save_samples(step)
            if self.config.dataset == 'GaussianMix':
                self.make_video(step, self.G_config, g_loss)
        if self.config.dataset == 'GaussianMix':
            self.G_config['writer'].finish()   
            
        coord.request_stop()
        coord.join(threads)
        self.sess.close()
        
                
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
        model_name = "MMDGAN.model"
        checkpoint_dir = os.path.join(checkpoint_dir, self.description)

        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        self.saver.save(self.sess,
                        os.path.join(checkpoint_dir, model_name),
                        global_step=step)


    def load(self, checkpoint_dir):
        print(" [*] Reading checkpoints...")

        checkpoint_dir = os.path.join(checkpoint_dir, self.description)

        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver.restore(self.sess, os.path.join(checkpoint_dir, ckpt_name))
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
            print(samples.shape)
            sample_dir = os.path.join(self.sample_dir, self.description)
            if not os.path.exists(sample_dir):
                os.makedirs(sample_dir)
            p = os.path.join(sample_dir, 'train_{:02d}.png'.format(step))
            save_images(samples[:64, :, :, :], [8, 8], p)  
            
    def get_samples(self, n=None, save=True):
        if not self.initialized_for_sampling:
            print('[*] Loading from ' + self.checkpoint_dir + '...')
            self.sess.run(tf.local_variables_initializer())
            self.sess.run(tf.global_variables_initializer())
            if self.load(self.checkpoint_dir):
                print(" [*] Load SUCCESS, model trained up to epoch %d" % \
                      self.sess.run(self.global_step))
            else:
                print(" [!] Load failed...")
                return
        if n is None:
            n = self.sample_size
        sampled = []
        while len(sampled) * self.sample_size < n:
            sampled.append(self.sess.run(self.G))
        samples = np.concatenate(sampled, axis=0)[:n]
        if not save:
            return samples
        file = os.path.join(self.sample_dir, self.description, 'samples.npy')
        np.save(file, samples, allow_pickle=False)
        print(" [*] %d samples saved in '%s'" % (n, file))
    
    
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