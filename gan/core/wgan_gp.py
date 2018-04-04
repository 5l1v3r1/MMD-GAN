from .model import MMD_GAN, tf


class WGAN_GP(MMD_GAN):
    def __init__(self, sess, config, **kwargs):
        config.dof_dim = 1
        super(WGAN_GP, self).__init__(sess, config, **kwargs)
        
    def set_loss(self, G, images):
        alpha = tf.random_uniform(shape=[self.batch_size, 1, 1, 1])
        real_data = self.images
        fake_data = self.G
        differences = fake_data - real_data
        interpolates0 = real_data + (alpha*differences)
        interpolates = self.discriminator(interpolates0, self.batch_size)

        gradients = tf.gradients(interpolates, [interpolates0])[0]
        slopes = tf.sqrt(tf.reduce_sum(tf.square(gradients), reduction_indices=[1]))
        gradient_penalty = tf.reduce_mean((slopes-1.)**2)

        self.gp = tf.get_variable('gradient_penalty', dtype=tf.float32,
                                  initializer=self.config.gradient_penalty)

        self.d_loss = tf.reduce_mean(G) - tf.reduce_mean(images) + self.gp * gradient_penalty
        self.g_loss = -tf.reduce_mean(G)
        self.optim_name = 'wgan_gp%d_loss' % int(self.config.gradient_penalty)

        tf.summary.scalar(self.optim_name + ' G', self.g_loss)
        tf.summary.scalar(self.optim_name + ' D', self.d_loss)

class SWGAN(MMD_GAN):
    def __init__(self, sess, config, **kwargs):
        config.dof_dim = 1
        super(SWGAN, self).__init__(sess, config, **kwargs)
        
    def set_loss(self, G, images):
        interpolates0 = self.images
        interpolates = self.discriminator(interpolates0, self.batch_size)

        self.unscaled_d_loss = tf.reduce_mean(G) - tf.reduce_mean(images)
        self.unscaled_g_loss = -self.unscaled_d_loss
        self.scale = tf.sqrt( self.hs*tf.reduce_mean(tf.square(interpolates)) + 1)
        self.d_loss = self.unscaled_d_loss/self.scale
        self.g_loss = -self.d_loss
        self.optim_name = 'SWGAN %d_loss' % int(self.config.gradient_penalty)

        tf.summary.scalar(self.optim_name + ' G', self.g_loss)
        tf.summary.scalar(self.optim_name + ' D', self.d_loss)
        tf.summary.scalar(self.optim_name + '_unscaled_G', self.unscaled_g_loss)
        tf.summary.scalar('dx_scale', self.scale)

class Squared_SWGAN(MMD_GAN):
    def __init__(self, sess, config, **kwargs):
        config.dof_dim = 1
        super(Squared_SWGAN, self).__init__(sess, config, **kwargs)
        
    def set_loss(self, G, images):
        interpolates0 = self.images
        interpolates = self.discriminator(interpolates0, self.batch_size)

        self.g_loss = tf.square(tf.reduce_mean(G) - tf.reduce_mean(images))/( self.hs*tf.reduce_mean(tf.square(interpolates))  + 1)
        self.d_loss = -self.g_loss
        self.optim_name = 'SWGAN %d_loss' % int(self.config.gradient_penalty)

        tf.summary.scalar(self.optim_name + ' G', self.g_loss)
        tf.summary.scalar(self.optim_name + ' D', self.d_loss)





