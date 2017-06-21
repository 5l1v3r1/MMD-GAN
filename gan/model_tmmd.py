import mmd as MMD

from model_mmd import DCGAN, tf, np
from utils import variable_summaries

class tmmd_DCGAN(DCGAN):
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
        super(tmmd_DCGAN, self).__init__(sess=sess, config=config, is_crop=is_crop,
             batch_size=batch_size, output_size=output_size, z_dim=z_dim, 
             gfc_dim=gfc_dim, dfc_dim=dfc_dim, c_dim=c_dim, 
             dataset_name=dataset_name, checkpoint_dir=checkpoint_dir,
             sample_dir=sample_dir, log_dir=log_dir, data_dir=data_dir)


    def set_loss(self, G, images):
        if self.config.kernel == 'rbf': # Gaussian kernel
            bandwidths = [1., 2., 4., 8. ,16.]
            tmmd2 = lambda gg, ii: MMD.mix_rbf_mmd2_and_ratio(
                gg, ii, sigmas=bandwidths)
        elif self.config.kernel == 'Euclidean': # -||x-y||^2 kenrel
            tmmd2 = lambda gg, ii: MMD.Euclidean_mmd2_and_ratio(gg, ii)    
        elif self.config.kernel == 'rq': # Rational quadratic kernel
            alphas = [.001, .01, .1, 1.0, 10.0]
            tmmd2 = lambda gg, ii: MMD.mix_rq_mmd2_and_ratio(
                gg, ii, alphas=alphas)
        elif self.config.kernel == 'di': # Distance - induced kernel
            self.di_kernel_z_images = tf.placeholder(
                tf.float32, 
                [self.batch_size, self.output_size, self.output_size, self.c_dim],
                name='di_kernel_z_images'
            )
            alphas = [1.0]
            di_r = np.random.choice(np.arange(self.batch_size))
            if self.config.dc_discriminator:
                self.di_kernel_z = self.discriminator(
                        self.di_kernel_z_images, reuse=True)[di_r: di_r + 1]
            else:
                self.di_kernel_z = tf.reshape(self.di_kernel_z_images[di_r: di_r + 1], [1, -1])
            tmmd2 = lambda gg, ii: MMD.mix_di_mmd2_and_ratio(
                    gg, ii, self.di_kernel_z, alphas=alphas)
        else:
            raise Exception("Kernel '%s' not implemented for %s model" % 
                            (self.config.kernel, self.config.model))
        
        with tf.variable_scope('loss'):
            self.kernel_loss, self.ratio_loss, self.var_est = tmmd2(G, images)
            tf.summary.scalar("kernel_loss", self.kernel_loss)
            tf.summary.scalar("ratio_loss", self.ratio_loss)
            self.kernel_loss = tf.sqrt(self.kernel_loss)
            self.optim_loss = tf.sqrt(self.ratio_loss)
            self.optim_name = 'ratio loss'
            
#            variable_summaries([(tf.clip_by_value(self.var_est, 0, 100000.0), 
#                                 'variance_estimate')])
            
            self.add_gradient_penalty(lambda gg, ii: tf.sqrt(tmmd2(gg, ii)[1]), G, images)    