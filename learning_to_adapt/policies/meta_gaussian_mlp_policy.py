from meta_mb.policies.gaussian_mlp_policy import GaussianMLPPolicy
import numpy as np
import tensorflow as tf
from meta_mb.utils.networks.mlp import forward_mlp


class MetaGaussianMLPPolicy(GaussianMLPPolicy, MetaPolicy):
    def __init__(self, meta_batch_size,  *args, **kwargs):
        self.quick_init(locals()) # store init arguments for serialization
        self.meta_batch_size = meta_batch_size

        self.pre_update_action_var = None
        self.pre_update_mean_var = None
        self.pre_update_log_std_var = None

        self.post_update_action_var = None
        self.post_update_mean_var = None
        self.post_update_log_std_var = None

        super(MetaGaussianMLPPolicy, self).__init__(*args, **kwargs)

    def build_graph(self):
        """
        Builds computational graph for policy
        """

        # Create pre-update policy by calling build_graph of the super class
        super(MetaGaussianMLPPolicy, self).build_graph()
        self.pre_update_action_var = tf.split(self.action_var, self.meta_batch_size)
        self.pre_update_mean_var = tf.split(self.mean_var, self.meta_batch_size)
        self.pre_update_log_std_var = [self.log_std_var for _ in range(self.meta_batch_size)]

        # Create lightweight policy graph that takes the policy parameters as placeholders
        with tf.variable_scope(self.name + "_ph_graph"):
            mean_network_phs_meta_batch, log_std_network_phs_meta_batch = [], []

            self.post_update_action_var = []
            self.post_update_mean_var = []
            self.post_update_log_std_var = []

            # build meta_batch_size graphs for post-update policies --> thereby the policy parameters are placeholders
            obs_var_per_task = tf.split(self.obs_var, self.meta_batch_size, axis=0)

            for idx in range(self.meta_batch_size):
                with tf.variable_scope("task_%i" % idx):

                    with tf.variable_scope("mean_network"):
                        # create mean network parameter placeholders
                        mean_network_phs = self._create_placeholders_for_vars(
                            scope=self.name + "/mean_network")  # -> returns ordered dict
                        mean_network_phs_meta_batch.append(mean_network_phs)

                        # forward pass through the mean mpl
                        _, mean_var = forward_mlp(output_dim=self.action_dim,
                                                  hidden_sizes=self.hidden_sizes,
                                                  hidden_nonlinearity=self.hidden_nonlinearity,
                                                  output_nonlinearity=self.output_nonlinearity,
                                                  input_var=obs_var_per_task[idx],
                                                  mlp_params=mean_network_phs,
                                                  )

                    with tf.variable_scope("log_std_network"):
                        # create log_stf parameter placeholders
                        log_std_network_phs = self._create_placeholders_for_vars(scope=self.name + "/log_std_network") # -> returns ordered dict
                        log_std_network_phs_meta_batch.append(log_std_network_phs)

                        log_std_var = list(log_std_network_phs.values())[0]  # weird stuff since log_std_network_phs is ordered dict

                    action_var = mean_var + tf.random_normal(shape=tf.shape(mean_var)) * tf.exp(log_std_var)

                    self.post_update_action_var.append(action_var)
                    self.post_update_mean_var.append(mean_var)
                    self.post_update_log_std_var.append(log_std_var)

            # merge mean_network_phs and log_std_network_phs into policies_params_phs
            self.policies_params_phs = []
            for idx, odict in enumerate(mean_network_phs_meta_batch): # Mutate mean_network_ph here
                odict.update(log_std_network_phs_meta_batch[idx])
                self.policies_params_phs.append(odict)

            self.policy_params_keys = list(self.policies_params_phs[0].keys())

    def get_action(self, observation, task=0):
        """
        Runs a single observation through the specified policy and samples an action

        Args:
            observation (ndarray) : single observation - shape: (obs_dim,)

        Returns:
            (ndarray) : single action - shape: (action_dim,)
        """
        observation = np.repeat(np.expand_dims(np.expand_dims(observation, axis=0), axis=0), self.meta_batch_size, axis=0)
        action, agent_infos = self.get_actions(observation)
        action, agent_infos = action[task][0], dict(mean=agent_infos[task][0]['mean'], log_std=agent_infos[task][0]['log_std'])
        return action, agent_infos

    def get_actions(self, observations):
        """
        Args:
            observations (list): List of numpy arrays of shape (meta_batch_size, batch_size, obs_dim)

        Returns:
            (tuple) : A tuple containing a list of numpy arrays of action, and a list of list of dicts of agent infos
        """
        assert len(observations) == self.meta_batch_size

        if self._pre_update_mode:
            actions, agent_infos = self._get_pre_update_actions(observations)
        else:
            actions, agent_infos = self._get_post_update_actions(observations)


        assert len(actions) == self.meta_batch_size
        return actions, agent_infos

    def _get_pre_update_actions(self, observations):
        """
        Args:
            observations (list): List of numpy arrays of shape (meta_batch_size, batch_size, obs_dim)

        """
        batch_size = observations[0].shape[0]
        assert all([obs.shape[0] == batch_size for obs in observations])
        assert len(observations) == self.meta_batch_size
        obs_stack = np.concatenate(observations, axis=0)
        feed_dict = {self.obs_var: obs_stack}

        sess = tf.get_default_session()
        actions, means, log_stds = sess.run([self.pre_update_action_var,
                                             self.pre_update_mean_var,
                                             self.pre_update_log_std_var],
                                            feed_dict=feed_dict)
        log_stds = np.concatenate(log_stds) # Get rid of fake batch size dimension (would be better to do this in tf, if we can match batch sizes)
        agent_infos = [[dict(mean=mean, log_std=log_stds[idx]) for mean in means[idx]] for idx in range(self.meta_batch_size)]
        return actions, agent_infos

    def _get_post_update_actions(self, observations):
        """
        Args:
            observations (list): List of numpy arrays of shape (meta_batch_size, batch_size, obs_dim)

        """
        assert self.policies_params_vals is not None
        obs_stack = np.concatenate(observations, axis=0)
        feed_dict = {self.obs_var: obs_stack}
        feed_dict.update(self.policies_params_feed_dict)

        sess = tf.get_default_session()
        actions, means, log_stds = sess.run([self.post_update_action_var,
                                             self.post_update_mean_var,
                                             self.post_update_log_std_var],
                                            feed_dict=feed_dict)
        log_stds = np.concatenate(log_stds) # Get rid of fake batch size dimension (would be better to do this in tf, if we can match batch sizes)
        agent_infos = [[dict(mean=mean, log_std=log_stds[idx]) for mean in means[idx]] for idx in range(self.meta_batch_size)]
        return actions, agent_infos


class MetaPolicy(Policy):

    def __init__(self, *args, **kwargs):
        super(MetaPolicy, self).__init__(*args, **kwargs)
        self._pre_update_mode = True
        self.policies_params_vals = None
        self.policy_params_keys = None
        self.policies_params_phs = None
        self.meta_batch_size = None

    def build_graph(self):
        """
        Also should create lists of variables and corresponding assign ops
        """
        raise NotImplementedError

    def switch_to_pre_update(self):
        """
        Switches get_action to pre-update policy
        """
        self._pre_update_mode = True
        # replicate pre-update policy params meta_batch_size times
        self.policies_params_vals = [self.get_param_values() for _ in range(self.meta_batch_size)]

    def get_actions(self, observations):
        if self._pre_update_mode:
            return self._get_pre_update_actions(observations)
        else:
            return self._get_post_update_actions(observations)

    def _get_pre_update_actions(self, observations):
        """
        Args:
            observations (list): List of size meta-batch size with numpy arrays of shape batch_size x obs_dim
        """
        raise NotImplementedError

    def _get_post_update_actions(self, observations):
        """
        Args:
            observations (list): List of size meta-batch size with numpy arrays of shape batch_size x obs_dim
        """
        raise NotImplementedError

    def update_task_parameters(self, updated_policies_parameters):
        """
        Args:
            updated_policies_parameters (list): List of size meta-batch size. Each contains a dict with the policies
            parameters as numpy arrays
        """
        self.policies_params_vals = updated_policies_parameters
        self._pre_update_mode = False

    def _create_placeholders_for_vars(self, scope, graph_keys=tf.GraphKeys.TRAINABLE_VARIABLES):
        var_list = tf.get_collection(graph_keys, scope=scope)
        placeholders = []
        for var in var_list:
            var_name = remove_scope_from_name(var.name, scope.split('/')[0])
            placeholders.append((var_name, tf.placeholder(tf.float32, shape=var.shape, name="%s_ph" % var_name)))
        return OrderedDict(placeholders)

    @property
    def policies_params_feed_dict(self):
        """
            returns fully prepared feed dict for feeding the currently saved policy parameter values
            into the lightweight policy graph
        """
        return dict(list((self.policies_params_phs[i][key], self.policies_params_vals[i][key])
                         for key in self.policy_params_keys for i in range(self.meta_batch_size)))
