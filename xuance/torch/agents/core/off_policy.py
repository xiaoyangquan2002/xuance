from tqdm import tqdm
import numpy as np
from copy import deepcopy
from typing import Optional
from argparse import Namespace
from xuance.torch.agents.base import Agent
from xuance.environment import DummyVecEnv
from xuance.common import DummyOffPolicyBuffer, DummyOffPolicyBuffer_Atari


class OffPolicyAgent(Agent):
    """The core class for on-policy algorithm with single agent.

    Args:
        config: the Namespace variable that provides hyper-parameters and other settings.
        envs: the vectorized environments.
    """
    def __init__(self,
                 config: Namespace,
                 envs: DummyVecEnv):
        super(OffPolicyAgent, self).__init__(config, envs)
        self.memory: Optional[DummyOffPolicyBuffer] = None

    def _build_memory(self, auxiliary_info_shape=None):
        self.atari = True if self.config.env_name == "Atari" else False
        Buffer = DummyOffPolicyBuffer_Atari if self.atari else DummyOffPolicyBuffer
        input_buffer = dict(observation_space=self.observation_space,
                            action_space=self.action_space,
                            auxiliary_shape=auxiliary_info_shape,
                            n_envs=self.n_envs,
                            buffer_size=self.config.buffer_size,
                            batch_size=self.config.batch_size)
        return Buffer(**input_buffer)

    def _build_policy(self):
        raise NotImplementedError

    def action(self, observations: np.ndarray,
               e_greedy: float = None, noise_scale: float = None):
        """Returns actions and values.

        Parameters:
            observations (np.ndarray): The observation.
            e_greedy (float): The epsilon greedy.
            noise_scale (float): The scale of noise.

        Returns:
            actions: The actions to be executed.
            values: The evaluated values.
            dists: The policy distributions.
            log_pi: Log of stochastic actions.
        """
        _, actions_output, _ = self.policy(observations)
        if e_greedy is not None:
            random_actions = np.random.choice(self.action_space.n, self.n_envs)
            if np.random.rand() < e_greedy:
                actions = random_actions
            else:
                actions = actions_output.detach().cpu().numpy()
        elif noise_scale is not None:
            actions = actions_output + np.random.normal(size=actions_output.shape) * noise_scale
        else:
            actions = actions_output
        return {"actions": actions}

    def train_epochs(self, n_epochs=1):
        train_info = {}
        for _ in range(n_epochs):
            samples = self.memory.sample()
            train_info = self.learner.update(**samples)
        if hasattr(self, "egreedy"):
            train_info["epsilon-greedy"] = self.egreedy
        elif hasattr(self, "noise_scale"):
            train_info["noise_scale"] = self.noise_scale
        else:
            pass
        return train_info

    def train(self, train_steps):
        obs = self.envs.buf_obs
        for _ in tqdm(range(train_steps)):
            step_info = {}
            self.obs_rms.update(obs)
            obs = self._process_observation(obs)
            policy_out = self.action(obs, self.egreedy)
            acts = policy_out['actions']
            next_obs, rewards, terminals, trunctions, infos = self.envs.step(acts)

            self.memory.store(obs, acts, self._process_reward(rewards), terminals, self._process_observation(next_obs))
            if self.current_step > self.start_training and self.current_step % self.training_frequency == 0:
                step_info = self.train_epochs(n_epochs=self.n_epochs)
                self.log_infos(step_info, self.current_step)

            obs = deepcopy(next_obs)
            for i in range(self.n_envs):
                if terminals[i] or trunctions[i]:
                    if self.atari and (~trunctions[i]):
                        pass
                    else:
                        obs[i] = infos[i]["reset_obs"]
                        self.envs.buf_obs[i] = obs[i]
                        self.current_episode[i] += 1
                        if self.use_wandb:
                            step_info["Episode-Steps/env-%d" % i] = infos[i]["episode_step"]
                            step_info["Train-Episode-Rewards/env-%d" % i] = infos[i]["episode_score"]
                        else:
                            step_info["Episode-Steps"] = {"env-%d" % i: infos[i]["episode_step"]}
                            step_info["Train-Episode-Rewards"] = {"env-%d" % i: infos[i]["episode_score"]}
                        self.log_infos(step_info, self.current_step)

            self.current_step += self.n_envs
            if self.egreedy >= self.end_greedy:
                self.egreedy -= self.delta_egreedy

    def test(self, env_fn, test_episodes):
        test_envs = env_fn()
        num_envs = test_envs.num_envs
        videos, episode_videos = [[] for _ in range(num_envs)], []
        current_episode, scores, best_score = 0, [], -np.inf
        obs, infos = test_envs.reset()
        if self.config.render_mode == "rgb_array" and self.render:
            images = test_envs.render(self.config.render_mode)
            for idx, img in enumerate(images):
                videos[idx].append(img)

        while current_episode < test_episodes:
            self.obs_rms.update(obs)
            obs = self._process_observation(obs)
            policy_out = self.action(obs)
            next_obs, rewards, terminals, trunctions, infos = test_envs.step(policy_out['actions'])
            if self.config.render_mode == "rgb_array" and self.render:
                images = test_envs.render(self.config.render_mode)
                for idx, img in enumerate(images):
                    videos[idx].append(img)

            obs = deepcopy(next_obs)
            for i in range(num_envs):
                if terminals[i] or trunctions[i]:
                    if self.atari and (~trunctions[i]):
                        pass
                    else:
                        obs[i] = infos[i]["reset_obs"]
                        scores.append(infos[i]["episode_score"])
                        current_episode += 1
                        if best_score < infos[i]["episode_score"]:
                            best_score = infos[i]["episode_score"]
                            episode_videos = videos[i].copy()
                        if self.config.test_mode:
                            print("Episode: %d, Score: %.2f" % (current_episode, infos[i]["episode_score"]))

        if self.config.render_mode == "rgb_array" and self.render:
            # time, height, width, channel -> time, channel, height, width
            videos_info = {"Videos_Test": np.array([episode_videos], dtype=np.uint8).transpose((0, 1, 4, 2, 3))}
            self.log_videos(info=videos_info, fps=self.fps, x_index=self.current_step)

        if self.config.test_mode:
            print("Best Score: %.2f" % (best_score))

        test_info = {
            "Test-Episode-Rewards/Mean-Score": np.mean(scores),
            "Test-Episode-Rewards/Std-Score": np.std(scores)
        }
        self.log_infos(test_info, self.current_step)

        test_envs.close()

        return scores

