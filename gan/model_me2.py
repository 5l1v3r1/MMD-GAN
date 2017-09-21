import mmd as MMD

from model_mmd2 import MMD_GAN, tf, np
from utils import variable_summaries
from cholesky import me_loss
from ops import batch_norm, conv2d, deconv2d, linear, lrelu
from glob import glob
import os
import time

class ME_GAN(MMD_GAN):
    def __init__(self, sess, config, is_crop=True,
                 batch_size=64, output_size=64,
                 z_dim=100, 
                 gfc_dim=1024, dfc_dim=1024, c_dim=3, dataset_name='default',
                 checkpoint_dir=None, sample_dir=None, log_dir=None, data_dir=None):
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
        self.asi = [np.zeros([batch_size, output_size, output_size, c_dim])]
        super(ME_GAN, self).__init__(sess=sess, config=config, is_crop=is_crop,
             batch_size=batch_size, output_size=output_size, z_dim=z_dim, 
             gfc_dim=gfc_dim, dfc_dim=dfc_dim, 
             c_dim=c_dim, dataset_name=dataset_name, checkpoint_dir=checkpoint_dir,
             sample_dir=sample_dir, log_dir=log_dir, data_dir=data_dir)
        
#    def test_location_initializer(self):
#        if 'lsun' in self.config.dataset:
##            generator = self.gen_train_samples_from_lmdb()
#            data_X = self.additional_sample_images
#        if self.config.dataset == 'mnist':
#            data_X, data_y = self.load_mnist()
#        elif self.config.dataset == 'cifar10':
#            data_X, data_y = self.load_cifar10()
#        elif (self.config.dataset == 'GaussianMix'):
#            data_X, _, __ = self.load_GaussianMix()
#        else:
#            data_X = glob(os.path.join("./data", self.config.dataset, "*.jpg"))
#        real = np.asarray(data_X[:self.batch_size], dtype=np.float32)
#        return real
##        sample_z = np.random.uniform(-1, 1, size=(self.sample_size , self.z_dim))
##        fake = self.sess.run(self.sampler, feed_dict={self.z: sample_z})
##        p = np.random.binomial(1, .5, size=(self.batch_size, 1, 1, 1))
##        return p * real + (1 - p) * fake
        
    def set_loss(self, G, images):
        if self.config.kernel == '':
            me = lambda gg, ii: me_loss(
                gg, ii, self.df_dim, self.batch_size,
                with_inv=(self.config.gradient_penalty == 0)
            )
            self.optim_name = 'me loss'
            with tf.variable_scope('loss'):
                self.mmd_loss, Z = me(G, images)
        else:
            im_id = tf.constant(np.random.choice(np.arange(self.batch_size), self.config.test_locations))
            if 'optme' in self.config.model:
                with tf.variable_scope('discriminator'):
                    self.me_test_images = tf.get_variable(
                        'd_me_test_images', 
#                        [self.batch_size, self.output_size, self.output_size, self.c_dim],
                        initializer=self.additional_sample_images
                    )
                p = tf.cast(tf.reshape(tf.multinomial([[.5, .5]], self.batch_size), 
                                       [self.batch_size, 1, 1, 1]), tf.float32)
                self.me_test_images = p * self.me_test_images + (1 - p) * self.sampler
                meti = tf.clip_by_value(tf.gather(self.me_test_images, im_id), 0, 1)
                bloc = int(np.floor(np.sqrt(self.config.test_locations)))
                tf.summary.image("train/me test image", self.imageRearrange(meti, bloc))
            else:
                self.me_test_images = tf.constant(
                    value=self.additional_sample_images,
                    dtype=tf.float32,
                    name='me_test_images'
                )
            if self.config.dc_discriminator:
                metl = self.discriminator(self.me_test_images, reuse=True)
            else:
                metl = tf.reshape(self.me_test_images, [self.batch_size, -1])
            self.me_test_locations = tf.gather(metl, im_id)
            
            assert self.config.kernel in ['dot', 'mix_rq', 'mix_rbf', 'distance'], \
                "Kernel '%s' not supported" % self.config.kernel
            kernel = getattr(MMD, '_%s_kernel' % self.config.kernel)
            k_test = lambda gg: kernel(gg, self.me_test_locations, K_XY_only=True)
            self.optim_name = self.config.kernel + ' kernel mean embedding loss'
            with tf.variable_scope('loss'):
                self.mmd_loss, Z = me_loss(
                    k_test(G), k_test(images), 
                    self.config.test_locations, self.batch_size, #TODO: df_dim not always ok !!!!!!!!
                    with_inv=('vn' in self.config.suffix),
                    with_Z=True
                )
            if 'full_gp' in self.config.suffix:
                super(ME_GAN, self).add_gradient_penalty(kernel, G, images)
            else:
                self.add_gradient_penalty(k_test, G, images, Z)

    def add_gradient_penalty(self, k_test, fake_data, real_data, Z):
        alpha = tf.random_uniform(shape=[self.batch_size, 1], minval=0., maxval=1.)
        if 'mid' in self.config.suffix:
            alpha = .4 + .2 * alpha
        elif 'edges' in self.config.suffix:
            qq = tf.cast(tf.reshape(tf.multinomial([[.5, .5]], self.batch_size),
                                    [self.batch_size, 1]), tf.float32)
            alpha = .1 * alpha * qq + (1. - .1 * alpha) * (1. - qq)
        elif 'edge' in self.config.suffix:
            alpha = .99 + .01 * alpha
        x_hat = (1. - alpha) * real_data + alpha * fake_data
        witness = tf.matmul(k_test(x_hat), Z)
        gradients = tf.gradients(witness, [x_hat])[0]
        penalty = tf.reduce_mean(tf.square(tf.norm(gradients, axis=1) - 1.0))
        
        if self.config.gradient_penalty > 0:
            self.gp = tf.get_variable('gradient_penalty', dtype=tf.float32,
                                      initializer=self.config.gradient_penalty)
            self.g_loss = self.mmd_loss
            self.d_loss = -self.mmd_loss + penalty * self.gp
            self.optim_name += ' gp %.1f' % self.config.gradient_penalty
        else:
            self.g_loss = self.mmd_loss
            self.d_loss = -self.mmd_loss
        # variable_summaries([(gradients, 'dx_gradients')])
        tf.summary.scalar(self.optim_name + ' G', self.g_loss)
        tf.summary.scalar(self.optim_name + ' D', self.d_loss)
        tf.summary.scalar('dx_penalty', penalty)


    def train_step(self):
        step = self.sess.run(self.global_step)
        write_summary = ((np.mod(step, 50) == 0) and (step < 1000)) \
                    or (np.mod(step, 1000) == 0) or (self.err_counter > 0)
        write_summary = True
        print('d, g = %d, %d' % (self.d_counter, self.g_counter))
        if self.config.use_kernel:
            eval_ops = [self.g_loss, self.d_loss]
            if self.config.is_demo:
                summary_str, step, g_loss, d_loss = self.sess.run(
                    [self.TrainSummary] + eval_ops
                )
            else:
                if self.d_counter == 0:
                    if write_summary:
                        _, summary_str, g_loss, d_loss = self.sess.run(
                            [self.g_grads, self.TrainSummary] + eval_ops
                        )
                    else:
                        _, g_loss, d_loss = self.sess.run([self.g_grads] + eval_ops)
                else:
                    _, g_loss, d_loss = self.sess.run([self.d_grads] + eval_ops)
            et = "[%2d] time: %4.4f" % (step, time.time() - self.start_time)
            assert ~np.isnan(g_loss), "NaN g_loss, epoch: " + et
            assert ~np.isnan(d_loss), "NaN d_loss, epoch: " + et              
        if self.d_counter == 0:
            if write_summary:
                try:
                    self.writer.add_summary(summary_str, step)
                    self.err_counter = 0
                except Exception as e:
                    print('Step %d summary exception. ' % step, e)
                    self.err_counter += 1
                    
                print("Epoch: [%2d] time: %4.4f, %s, G: %.8f, D: %.8f"
                    % (step, time.time() - self.start_time, 
                       self.optim_name, g_loss, d_loss)) 
            if (np.mod(step + 1, self.config.max_iteration//5) == 0):
                self.lr *= self.config.decay_rate
                print('current learning rate: %f' % self.sess.run(self.lr))
                if ('decay_gp' in self.config.suffix) and (self.config.gradient_penalty > 0):
                    self.gp *= self.config.decay_rate
                    print('current gradient penalty: %f' % self.sess.run(self.gp))
                    
        if (step == 1) and (self.d_counter == 0):
            print('current learning rate: %f' % self.sess.run(self.lr))
        if (self.g_counter == 0) and (self.d_grads is not None):
            d_steps = self.config.dsteps
            if ((step % 100 == 0) or (step < 20)):
                d_steps = self.config.start_dsteps
            self.d_counter = (self.d_counter + 1) % (d_steps + 1)
        if self.d_counter == 0:
            self.g_counter = (self.g_counter + 1) % self.config.gsteps
        
        return g_loss, d_loss, step
    