from __future__ import division, print_function
from glob import glob
import os
import time

import numpy as np
import scipy.misc
from six.moves import xrange
import tensorflow as tf
import matplotlib.pyplot as plt

import mmd
from ops import batch_norm, conv2d, deconv2d, linear, lrelu
from utils import save_images, unpickle, read_and_scale, center_and_scale

class DCGAN(object):
    def __init__(self, sess, config, is_crop=True,
                 batch_size=64, output_size=64,
                 z_dim=100, gf_dim=4, df_dim=4,
                 gfc_dim=1024, dfc_dim=1024, c_dim=3, dataset_name='default',
                 checkpoint_dir=None, sample_dir=None, log_dir=None, 
                 data_dir=None, gradient_clip=1.0):
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
        self.sess = sess
        self.config = config
        self.is_crop = is_crop
        self.is_grayscale = (c_dim == 1)
        self.batch_size = batch_size
        self.sample_size = batch_size
#        if self.config.dataset == 'GaussianMix':
#            self.sample_size = min(16 * batch_size, 512)
        self.output_size = output_size
        self.sample_dir = sample_dir
        self.log_dir=log_dir
        self.checkpoint_dir = checkpoint_dir
        self.data_dir = data_dir
        self.z_dim = z_dim

        self.gf_dim = gf_dim
        self.df_dim = df_dim

        self.gfc_dim = gfc_dim
        self.dfc_dim = dfc_dim

        self.c_dim = c_dim

        # batch normalization : deals with poor initialization helps gradient flow
#        self.d_bn1 = batch_norm(name='d_bn1')
#        self.d_bn2 = batch_norm(name='d_bn2')
#        self.d_bn3 = batch_norm(name='d_bn3')

        self.g_bn0 = batch_norm(name='g_bn0')
        self.g_bn1 = batch_norm(name='g_bn1')
        self.g_bn2 = batch_norm(name='g_bn2')
        self.g_bn3 = batch_norm(name='g_bn3')

        self.dataset_name = dataset_name
        
        discriminator_desc = '_dc' if self.config.dc_discriminator else ''
        self.description = ("%s_%s%s_%s_%s_%s_lr" % (self.dataset_name, 
                    self.config.architecture, discriminator_desc,
                    self.config.kernel, self.batch_size, self.output_size)) + \
                    str(self.config.learning_rate)
        self.build_model()


    def imageRearrange(self, image, block=4):
        image = tf.slice(image, [0, 0, 0, 0], [block * block, -1, -1, -1])
        x1 = tf.batch_to_space(image, [[0, 0], [0, 0]], block)
        image_r = tf.reshape(tf.transpose(tf.reshape(x1,
            [self.output_size, block, self.output_size, block, self.c_dim])
            , [1, 0, 3, 2, 4]),
            [1, self.output_size * block, self.output_size * block, self.c_dim])
        return image_r

        
    def build_model(self):
        self.global_step = tf.Variable(0, name="global_step", trainable=False)
        self.lr = tf.placeholder(tf.float32, shape=[])
        self.images = tf.placeholder(
            tf.float32, 
            [self.batch_size, self.output_size, self.output_size, self.c_dim],
            name='real_images'
        )
        self.sample_images = tf.placeholder(
            tf.float32, 
            [self.sample_size, self.output_size, self.output_size, self.c_dim],
            name='sample_images'
        )
        if self.config.kernel == 'di':
            self.di_kernel_z_images = tf.placeholder(
                tf.float32, 
                [self.batch_size, self.output_size, self.output_size, self.c_dim],
                name='di_kernel_z_images'
            )
        self.z = tf.placeholder(tf.float32, [None, self.z_dim], name='z')

        tf.summary.histogram("z", self.z)

        if self.config.dataset == 'cifar10':
            self.G = self.generator_cifar10(self.z)
        elif self.config.dataset == 'mnist':
            self.G = self.generator_mnist(self.z)
        elif 'lsun' in self.config.dataset:
            self.G = self.generator_lsun(self.z)
        elif self.config.dataset == 'GaussianMix':
            self.G = self.generator(self.z)
        else:
            raise ValueError("not implemented dataset '%s'" % self.config.dataset)
        if self.config.dc_discriminator:
            images = self.discriminator(self.images, reuse=False)
            G = self.discriminator(self.G, reuse=True)
        else:
            images = tf.reshape(self.images, [self.batch_size, -1])
            G = tf.reshape(self.G, [self.batch_size, -1])

        self.set_loss(G, images)

        block = min(8, int(np.sqrt(self.batch_size)))
        tf.summary.image("train/input image", 
                         self.imageRearrange(tf.clip_by_value(self.images, 0, 1), block))
        tf.summary.image("train/gen image", 
                         self.imageRearrange(tf.clip_by_value(self.G, 0, 1), block))

        if self.config.dataset == 'cifar10':
            self.sampler = self.generator_cifar10(self.z, is_train=False, reuse=True)
        elif self.config.dataset == 'mnist':
            self.sampler = self.generator_mnist(self.z, is_train=False, reuse=True)
        elif 'lsun' in self.config.dataset:
            self.sampler = self.generator_lsun(self.z, is_train=False, reuse=True)
        elif self.config.dataset == 'GaussianMix':
            self.sampler = self.generator(self.z, is_train=False, reuse=True)
        else:
            self.sampler = self.generator_any_set(self.z, is_train=False, reuse=True)
        t_vars = tf.trainable_variables()

        self.d_vars = [var for var in t_vars if 'd_' in var.name]
        self.g_vars = [var for var in t_vars if 'g_' in var.name]

        self.saver = tf.train.Saver()
        
    def set_loss(self, G, images):
        if self.config.kernel == 'rbf': # Gaussian kernel
            bandwidths = [2.0, 5.0, 10.0, 20.0, 40.0, 80.0]
            self.kernel_loss = mmd.mix_rbf_mmd2(G, images, sigmas=bandwidths)
        elif self.config.kernel == 'rq': # Rational quadratic kernel
            alphas = [.1, .2, .5, 1.0, 2.0]
            self.kernel_loss = mmd.mix_rq_mmd2(G, images, alphas=alphas)
        elif self.config.kernel == 'di': # Distance - induced kernel
            alphas = [1.0]
            di_r = np.random.choice(np.arange(self.batch_size))
            if self.config.dc_discriminator:
                self.di_kernel_z = self.discriminator(
                        self.di_kernel_z_images, reuse=True)[di_r: di_r + 1]
            else:
                self.di_kernel_z = tf.reshape(self.di_kernel_z_images[di_r: di_r + 1], [1, -1])
            self.kernel_loss = mmd.mix_di_mmd2(G, images, self.di_kernel_z, 
                                               alphas=alphas)
        else:
            raise Exception("Kernel '%s' not implemented" % self.config.kernel)
        tf.summary.scalar("kernel_loss", self.kernel_loss)
        self.kernel_loss = tf.sqrt(self.kernel_loss)
        self.optim_loss = self.kernel_loss
        self.optim_name = 'kernel loss'

    def train(self, config):
        """Train DCGAN"""
        if config.dataset == 'mnist':
            data_X, data_y = self.load_mnist()
        elif config.dataset == 'cifar10':
            data_X, data_y = self.load_cifar10()
        elif (config.dataset == 'GaussianMix'):
            data_X, ax1, wrtr = self.load_GaussianMix()
            g_line = None
            fig = ax1.figure
            from IPython.display import display
        else:
            data = glob(os.path.join("./data", config.dataset, "*.jpg"))
        if self.config.use_kernel:
            g_kernel_optim = tf.train.MomentumOptimizer(self.lr, 0.9)
            g_gvs = g_kernel_optim.compute_gradients(
                loss=self.optim_loss, 
                var_list=self.g_vars
            )            
            capped_g_gvs = [(tf.clip_by_value(g, -1., 1.), v) for g, v in g_gvs]
            g_apply_grads = g_kernel_optim.apply_gradients(
                capped_g_gvs, 
                global_step=self.global_step
            )
            if self.config.dc_discriminator:
                d_kernel_optim = tf.train.MomentumOptimizer(self.lr, 0.9)
                d_gvs = d_kernel_optim.compute_gradients(
                    loss=self.optim_loss, 
                    var_list=self.d_vars
                )
                # negative gradients for maximization wrt discriminator
                capped_d_gvs = [(-tf.clip_by_value(g, -1., 1.), v) for g, v in d_gvs]
                d_apply_grads = d_kernel_optim.apply_gradients(capped_d_gvs)    

        self.sess.run(tf.global_variables_initializer())
        TrainSummary = tf.summary.merge_all()
        log_dir = os.path.join(self.log_dir, self.description)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        self.writer = tf.summary.FileWriter(log_dir, self.sess.graph)

        sample_z = np.random.uniform(-1, 1, size=(self.sample_size , self.z_dim))

        if config.dataset in ['mnist', 'cifar10', 'GaussianMix']:
            sample_images = data_X[0:self.sample_size]
            di_kernel_z_images = data_X[0: self.batch_size]
        else:
           return
        counter = 1
        start_time = time.time()

        if self.load(self.checkpoint_dir):
            print(" [*] Load SUCCESS")
        else:
            print(" [!] Load failed...")

        if config.dataset in ['mnist', 'cifar10', 'GaussianMix']:
            batch_idxs = len(data_X) // config.batch_size
        else:
            data = glob(os.path.join("./data", config.dataset, "*.jpg"))
            batch_idxs = min(len(data), config.train_size) // config.batch_size
        lr = self.config.learning_rate
        for it in xrange(self.config.max_iteration):
            if np.mod(it, batch_idxs) == 0:
                perm = np.random.permutation(len(data_X))
            if np.mod(it, 10000) == 1:
                lr = lr * self.config.decay_rate
            idx = np.mod(it, batch_idxs)
            batch_images = data_X[perm[idx*config.batch_size:
                                       (idx+1)*config.batch_size]]

            batch_z = np.random.uniform(
                -1, 1, [config.batch_size, self.z_dim]).astype(np.float32)

            if self.config.use_kernel:
                feed_dict = {self.lr: lr, self.images: batch_images,
                             self.z: batch_z}
                if self.config.kernel == 'di':
                    feed_dict.update({self.di_kernel_z_images: di_kernel_z_images})
                if self.config.is_demo:
                    summary_str, step, optim_loss = self.sess.run(
                        [TrainSummary, self.global_step, self.optim_loss],
                        feed_dict=feed_dict
                    )
                else:
                    _, summary_str, step, optim_loss = self.sess.run(
                        [g_apply_grads, TrainSummary, self.global_step,
                         self.optim_loss], feed_dict=feed_dict
                    )
                    if self.config.dc_discriminator and \
                        (np.mod(counter, 2) == 1) and \
                        (counter < self.config.max_iteration * 3/4):
                        _, summary_str, step, optim_loss = self.sess.run(
                            [d_apply_grads, TrainSummary, self.global_step,
                             self.optim_loss], feed_dict=feed_dict
                        )                 

            counter += 1
            if np.mod(counter, 10) == 1:
                self.writer.add_summary(summary_str, step)
                print("Epoch: [%2d] time: %4.4f, %s: %.8f"
                    % (it, time.time() - start_time, self.optim_name, optim_loss))
#                print('D_VARS')
#                for var in self.d_vars:
#                    print(var.name, self.sess.run(var).mean())
#                print('G_VARS')
#                for var in self.g_vars:
#                    print(var.name, self.sess.run(var).mean())                
            if np.mod(counter, 500) == 1:
                self.save(self.checkpoint_dir, counter)
                samples = self.sess.run(self.sampler, feed_dict={
                    self.z: sample_z, self.images: sample_images})
                print(samples.shape)
                sample_dir = os.path.join(self.sample_dir, self.description)
                if not os.path.exists(sample_dir):
                    os.makedirs(sample_dir)
                p = os.path.join(sample_dir, 'train_{:02d}.png'.format(it))
                save_images(samples[:64, :, :, :], [8, 8], p)
            if (config.dataset == 'GaussianMix') and np.mod(counter, 20) == 1:          
                samples = self.sess.run(self.sampler, feed_dict={
                    self.z: sample_z, self.images: sample_images})
                if g_line is not None:
                    g_line.remove()
                g_line, = myhist(samples, ax=ax1, color='b')
                plt.title("Iteration {: 6}:, loss {:7.4f}".format(
                        counter, optim_loss))
                wrtr.grab_frame()
                if counter % 100 == 0:
                    display(fig)
        if config.dataset == 'GaussianMix':
            wrtr.finish()    

    def train_large(self, config):
        """Train DCGAN"""
        if self.config.use_kernel:
            kernel_optim = tf.train.MomentumOptimizer(self.lr, 0.9)
            gvs = kernel_optim.compute_gradients(
                loss=self.optim_loss, 
                var_list=self.g_vars + self.d_vars
            )
            capped_gvs = [(tf.clip_by_value(g, -1., 1.), v) for g, v in gvs]
            apply_grads = kernel_optim.apply_gradients(capped_gvs, 
                                                       global_step=self.global_step)
        self.sess.run(tf.global_variables_initializer())
        TrainSummary = tf.summary.merge_all()
        log_dir = os.path.join(self.log_dir, self.description)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        self.writer = tf.summary.FileWriter(log_dir, self.sess.graph)

        sample_z = np.random.uniform(-1, 1, size=(self.sample_size , self.z_dim))

        generator = self.gen_train_samples_from_lmdb()
        if 'lsun' in self.dataset_name:
            required_samples = int(np.ceil(self.sample_size/float(self.batch_size)))
            sampled = [next(generator) for _ in xrange(required_samples)]
            sample_images = np.concatenate(sampled, axis=0)[: self.sample_size]
            if self.config.kernel == 'di':
                di_kernel_z_images = next(generator)#sampled[0]
        else:
           return
        counter = 1
        start_time = time.time()

        if self.load(self.checkpoint_dir):
            print(" [*] Load SUCCESS")
        else:
            print(" [!] Load failed...")
        lr = self.config.learning_rate
        di_r = np.random.choice(np.arange(self.sample_size))
        for it in xrange(self.config.max_iteration):
            if np.mod(it, 10000) == 1:
                lr = lr * self.config.decay_rate
            batch_images = next(generator)
            batch_z = np.random.uniform(
                -1, 1, [config.batch_size, self.z_dim]).astype(np.float32)
            if self.config.use_kernel:
                feed_dict = {self.lr: lr, self.images: batch_images,
                             self.z: batch_z}
                if self.config.kernel == 'di':
                    feed_dict.update({self.di_kernel_z_images: di_kernel_z_images})
                if self.config.is_demo:
                    summary_str, step, optim_loss = self.sess.run(
                        [TrainSummary, self.global_step, self.optim_loss],
                        feed_dict=feed_dict)
                else:
                    _, summary_str, step, optim_loss = self.sess.run(
                        [apply_grads, TrainSummary, self.global_step,
                         self.optim_loss], feed_dict=feed_dict)

            counter += 1
            if np.mod(counter, 10) == 1:
                self.writer.add_summary(summary_str, step)
                print("Epoch: [%2d] time: %4.4f, %s: %.8f"
                    % (it, time.time() - start_time, optim_name, optim_loss))
            if np.mod(counter, 500) == 1:
                self.save(self.checkpoint_dir, counter)
                samples = self.sess.run(self.sampler, feed_dict={
                    self.z: sample_z, self.images: sample_images})
                print(samples.shape)
                sample_dir = os.path.join(self.sample_dir, self.description)
                if not os.path.exists(sample_dir):
                    os.makedirs(sample_dir)
                p = os.path.join(sample_dir, 'train_{:02d}.png'.format(it))
                save_images(samples[:64, :, :, :], [8, 8], p)
                
    def sampling(self, config):
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
            samples_z = np.random.uniform(-1, 1, size=(self.batch_size, self.z_dim))
            [G] = self.sess.run([self.G], feed_dict={self.z: samples_z})
            print("G shape", G.shape)
            for i in range(self.batch_size):
                G_tmp = np.zeros((28, 28, 3))
                G_tmp[:,:,:1] = G[i]
                G_tmp[:,:,1:2] = G[i]
                G_tmp[:,:,2:3] = G[i]

                n = i + batch_id * self.batch_size
                p = os.path.join(sample_dir, "img_{}.png".format(n))
                scipy.misc.imsave(p, G_tmp)


    def discriminator(self, image, y=None, reuse=False):
        with tf.variable_scope("discriminator") as scope:
            if reuse:
                scope.reuse_variables()
    
            s = self.output_size
            if True: #np.mod(s, 16) == 0:
                h0 = lrelu(conv2d(image, self.df_dim, name='d_h0_conv'))
#                h1 = lrelu(self.d_bn1(conv2d(h0, self.df_dim*2, name='d_h1_conv')))
#                h2 = lrelu(self.d_bn2(conv2d(h1, self.df_dim*4, name='d_h2_conv')))
#                h3 = lrelu(self.d_bn3(conv2d(h2, self.df_dim*8, name='d_h3_conv')))
                h1 = lrelu(batch_norm(name='d_bn1')(conv2d(h0, self.df_dim*2, name='d_h1_conv')))
                h2 = lrelu(batch_norm(name='d_bn2')(conv2d(h1, self.df_dim*4, name='d_h2_conv')))
                h3 = lrelu(batch_norm(name='d_bn3')(conv2d(h2, self.df_dim*8, name='d_h3_conv')))
#                h4 = linear(tf.reshape(h3, [self.batch_size, -1]), 1, 'd_h3_lin')
                return tf.reshape(h3, [self.batch_size, -1])
#                return tf.nn.sigmoid(h4), h4
            else:
                h0 = lrelu(conv2d(image, self.df_dim, name='d_h0_conv'))
                h1 = lrelu(self.d_bn1(conv2d(h0, self.df_dim*2, name='d_h1_conv')))
                h2 = linear(tf.reshape(h1, [self.batch_size, -1]), 1, 'd_h2_lin')
                if not self.config.use_kernel:
                    return h2
#                  return tf.nn.sigmoid(h2), h2
                else:
                  return tf.nn.sigmoid(h2), h2, h1, h0


    def generator_mnist(self, z, is_train=True, reuse=False):
        if reuse:
            tf.get_variable_scope().reuse_variables()
        h0 = linear(z, 64, 'g_h0_lin', stddev=self.config.init)
        h1 = linear(tf.nn.relu(h0), 256, 'g_h1_lin', stddev=self.config.init)
        h2 = linear(tf.nn.relu(h1), 256, 'g_h2_lin', stddev=self.config.init)
        h3 = linear(tf.nn.relu(h2), 1024, 'g_h3_lin', stddev=self.config.init)
        h4 = linear(tf.nn.relu(h3), 28 * 28 * 1, 'g_h4_lin', stddev=self.config.init)

        return tf.reshape(tf.nn.sigmoid(h4), [self.batch_size, 28, 28, 1])


    def generator_cifar10(self, z, is_train=True, reuse=False):
        if self.config.architecture == 'dc':
            return self.generator(z, is_train=is_train, reuse=reuse)
        if reuse:
            tf.get_variable_scope().reuse_variables()
        h0 = linear(z, 64, 'g_h0_lin', stddev=self.config.init)
        h1 = linear(tf.nn.relu(h0), 256, 'g_h1_lin', stddev=self.config.init)
        h2 = linear(tf.nn.relu(h1), 256, 'g_h2_lin', stddev=self.config.init)
        h3 = linear(tf.nn.relu(h2), 1024, 'g_h3_lin', stddev=self.config.init)
        h4 = linear(tf.nn.relu(h3), 32 * 32 * 3, 'g_h4_lin', stddev=self.config.init)

        return tf.reshape(tf.nn.sigmoid(h4), [self.batch_size, 32, 32, 3]) 


    def generator_any_set(self, z, is_train=True, reuse=False):
        if reuse:
            tf.get_variable_scope().reuse_variables()
        h0 = linear(z, 64, 'g_h0_lin', stddev=self.config.init)
        h1 = linear(tf.nn.relu(h0), 256, 'g_h1_lin', stddev=self.config.init)
        h2 = linear(tf.nn.relu(h1), 256, 'g_h2_lin', stddev=self.config.init)
        h3 = linear(tf.nn.relu(h2), 1024, 'g_h3_lin', stddev=self.config.init)
        h4 = linear(tf.nn.relu(h3), self.output_size**2 * self.c_dim, 
                    'g_h4_lin', stddev=self.config.init)

        return tf.reshape(tf.nn.sigmoid(h4), [self.batch_size, self.output_size, 
                                              self.output_size, self.c_dim])
        
    def generator_lsun(self, z, is_train=True, reuse=False):
        if self.config.architecture == 'dc':
            return self.generator(z, is_train=is_train, reuse=reuse)
        elif self.config.architecture == 'mlp':
            return self.generator_any_set(z, is_train=is_train, reuse=reuse)
        raise Exception("architecture '%s' not available" % self.config.architecture)
        
    def generator(self, z, y=None, is_train=True, reuse=False):
        if reuse:
            tf.get_variable_scope().reuse_variables()

        s = self.output_size
        if True: #np.mod(s, 16) == 0:
            s2, s4, s8, s16 = max(1, int(s/2)), max(1, int(s/4)), max(1, int(s/8)), max(1, int(s/16))

            # project `z` and reshape
            self.z_, self.h0_w, self.h0_b = linear(z, self.gf_dim*8*s16*s16, 'g_h0_lin', with_w=True)

            self.h0 = tf.reshape(self.z_, [-1, s16, s16, self.gf_dim * 8])
            h0 = lrelu(self.g_bn0(self.h0, train=is_train))

            self.h1, self.h1_w, self.h1_b = deconv2d(h0,
                [self.batch_size, s8, s8, self.gf_dim*4], name='g_h1', with_w=True)
            h1 = lrelu(self.g_bn1(self.h1, train=is_train))

            h2, self.h2_w, self.h2_b = deconv2d(h1,
                [self.batch_size, s4, s4, self.gf_dim*2], name='g_h2', with_w=True)
            h2 = lrelu(self.g_bn2(h2, train=is_train))

            h3, self.h3_w, self.h3_b = deconv2d(h2,
                [self.batch_size, s2, s2, self.gf_dim*1], name='g_h3', with_w=True)
            h3 = lrelu(self.g_bn3(h3, train=is_train))

            h4, self.h4_w, self.h4_b = deconv2d(h3,
                [self.batch_size, s, s, self.c_dim], name='g_h4', with_w=True)
            return h4
        else:
            s = self.output_size
            s2, s4 = int(s/2), int(s/4)
            self.z_, self.h0_w, self.h0_b = linear(z, self.gf_dim*2*s4*s4, 'g_h0_lin', with_w=True)

            self.h0 = tf.reshape(self.z_, [-1, s4, s4, self.gf_dim * 2])
            h0 = lrelu(self.g_bn0(self.h0, train=is_train))

            self.h1, self.h1_w, self.h1_b = deconv2d(h0,
                [self.batch_size, s2, s2, self.gf_dim*1], name='g_h1', with_w=True)
            h1 = lrelu(self.g_bn1(self.h1, train=is_train))

            h2, self.h2_w, self.h2_b = deconv2d(h1,
                [self.batch_size, s, s, self.c_dim], name='g_h2', with_w=True)

            return h2


    def load_mnist(self):
        data_dir = os.path.join(self.data_dir, self.dataset_name)

        fd = open(os.path.join(data_dir,'train-images-idx3-ubyte'))
        loaded = np.fromfile(file=fd,dtype=np.uint8)
        trX = loaded[16:].reshape((60000,28,28,1)).astype(np.float)

        fd = open(os.path.join(data_dir,'train-labels-idx1-ubyte'))
        loaded = np.fromfile(file=fd,dtype=np.uint8)
        trY = loaded[8:].reshape((60000)).astype(np.float)

        fd = open(os.path.join(data_dir,'t10k-images-idx3-ubyte'))
        loaded = np.fromfile(file=fd,dtype=np.uint8)
        teX = loaded[16:].reshape((10000,28,28,1)).astype(np.float)

        fd = open(os.path.join(data_dir,'t10k-labels-idx1-ubyte'))
        loaded = np.fromfile(file=fd,dtype=np.uint8)
        teY = loaded[8:].reshape((10000)).astype(np.float)

        trY = np.asarray(trY)
        teY = np.asarray(teY)

        X = np.concatenate((trX, teX), axis=0)
        y = np.concatenate((trY, teY), axis=0)

        seed = 547
        np.random.seed(seed)
        np.random.shuffle(X)
        np.random.seed(seed)
        np.random.shuffle(y)

        return X/255.,y


    def load_cifar10(self, categories=[0]):
        data_dir = os.path.join(self.data_dir, self.dataset_name)

        batchesX, batchesY = [], []
        for batch in range(1,6):
            loaded = unpickle(os.path.join(data_dir, 'data_batch_%d' % batch))
            idx = np.in1d(np.array(loaded['labels']), categories)
            batchesX.append(loaded['data'][idx].reshape(idx.sum(), 3, 32, 32))
            batchesY.append(np.array(loaded['labels'])[idx])
        trX = np.concatenate(batchesX, axis=0).transpose(0, 2, 3, 1)
        trY = np.concatenate(batchesY, axis=0)
        
        test = unpickle(os.path.join(data_dir, 'test_batch'))
        idx = np.in1d(np.array(test['labels']), categories)
        teX = test['data'][idx].reshape(idx.sum(), 3, 32, 32).transpose(0, 2, 3, 1)
        teY = np.array(test['labels'])[idx]

        X = np.concatenate((trX, teX), axis=0)
        y = np.concatenate((trY, teY), axis=0)

        seed = 547
        np.random.seed(seed)
        np.random.shuffle(X)
        np.random.seed(seed)
        np.random.shuffle(y)

        return X/255.,y


    def gen_train_samples_from_files(self):
        data_dir = os.path.join(self.data_dir, self.dataset_name)
        train_sample_files = os.listdir(data_dir)
        n_batches = len(train_sample_files) // self.batch_size
        train_sample_files = train_sample_files[:self.batch_size * n_batches]
        sampled = 0
        while True:
            train_sample_files = np.random.permutation(train_sample_files)
            batch_files_array = train_sample_files.reshape(n_batches, self.batch_size)
            for batch_files in batch_files_array:
                ims = [[read_and_scale(os.path.join("./data", self.dataset_name, f), 
                                       size=float(self.output_size))] for f in batch_files]
                ims = np.concatenate(ims, axis=0)
                sh = (self.batch_size, self.output_size, self.output_size, self.c_dim)
                assert ims.shape == sh, "wrong shape: " + repr(ims.shape)
                sampled += self.batch_size
                yield ims

                
    def gen_train_samples_from_lmdb(self):
        from PIL import Image
        import lmdb
        import io
        data_dir = os.path.join(self.data_dir, self.dataset_name)
        env = lmdb.open(data_dir, map_size=1099511627776, max_readers=100, readonly=True)
        sampled = 0
        buff, buff_lim = [], 1000
        sh = (self.batch_size, self.output_size, self.output_size, self.c_dim)
        while True:
            with env.begin(write=False) as txn:
                cursor = txn.cursor()
                for k, byte_arr in cursor:
                    im = Image.open(io.BytesIO(byte_arr))
                    buff.append(center_and_scale(im, size=self.output_size))
                    if len(buff) >= buff_lim:
                        buff = list(np.random.permutation(buff))
                        n_batches = max(1, len(buff)//(10 * self.batch_size))
                        for n in xrange(0, n_batches):
                            batch = np.array(buff[n * self.batch_size: (n + 1) * self.batch_size])
                            assert batch.shape == sh, "wrong shape: " + repr(batch.shape) + ", should be " + repr(sh)
                            sampled == self.batch_size
                            yield batch
                        buff = buff[n_batches * self.batch_size:]
        env.close()

        
    def load_GaussianMix(self, means=[.0, 3.0], stds=[1.0, .5], size=1000):
        from matplotlib import animation
        import sys
        X_real = np.r_[
            np.random.normal(0,  1, size=size),
            np.random.normal(3, .5, size=size),
        ]   
        X_real = X_real.reshape(X_real.shape[0], 1, 1, 1)
        
        xlo = -5
        xhi = 7
        
        ax1 = plt.gca()
        fig = ax1.figure
        ax1.grid(False)
        ax1.set_yticks([], [])
        myhist(X_real.ravel(), color='r')
        ax1.set_xlim(xlo, xhi)
        ax1.set_ylim(0, 1.05)
        ax1._autoscaleXon = ax1._autoscaleYon = False
        
        wrtr = animation.writers['ffmpeg'](fps=20)
        sample_dir = os.path.join(self.sample_dir, self.description)
        if not os.path.exists(sample_dir):
            os.makedirs(sample_dir)
        wrtr.setup(fig=fig, outfile=os.path.join(sample_dir, 'train.mp4'), dpi=100)
        return X_real, ax1, wrtr
            
    def save(self, checkpoint_dir, step):
        model_name = "DCGAN.model"
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
        
def myhist(X, ax=plt, bins='auto', **kwargs):
    hist, bin_edges = np.histogram(X, bins=bins)
    hist = hist / hist.max()
    return ax.plot(
        np.c_[bin_edges, bin_edges].ravel(),
        np.r_[0, np.c_[hist, hist].ravel(), 0],
        **kwargs
    )