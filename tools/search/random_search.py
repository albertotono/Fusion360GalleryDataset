import sys
import os
import random
import math
from pathlib import Path
import numpy as np


from search import Search


class RandomSearch(Search):

    def __init__(self, env):
        super().__init__(env)

    def search(self, agent, budget, score_function=None):
        super().search(agent, budget, score_function)
        # the length of rollout is the same as the number of planar faces as a maximum
        rollout_length = 0
        for node in self.target_graph["nodes"]:
            if node["surface_type"] == "PlaneSurfaceType":
                rollout_length += 1
        rollout_attempt = 0
        used_budget = 0
        max_score = 0
        max_scores = []

        while used_budget < budget:
            # We begin each rollout an empty graph
            cur_graph = self.env.get_empty_graph()
            for i in range(rollout_length):
                actions, action_probabilities = agent.get_actions_probabilities(cur_graph, self.target_graph)
                # Filter for clearly bad actions
                action_probabilities = self.filter_bad_actions(cur_graph, actions, action_probabilities)
                action = np.random.choice(actions, 1, p=action_probabilities)[0]
                new_graph, cur_iou = self.env.extrude(action["start_face"], action["end_face"], action["operation"])
                if cur_iou is not None:
                    max_score = max(max_score, cur_iou)
                if new_graph is not None:
                    cur_graph = new_graph
                self.log.log({
                    "rollout_attempt": rollout_attempt,
                    "rollout_step": i,
                    "rollout_length": rollout_length,
                    "used_budget": used_budget,
                    "budget": budget,
                    "start_face": action["start_face"],
                    "end_face": action["end_face"],
                    "operation": action["operation"],
                    "current_iou": cur_iou,
                    "max_iou": max_score
                })
                max_scores.append(max_score)
                used_budget += 1
            # Revert to the target and remove all reconstruction
            self.env.revert_to_target()
            rollout_attempt += 1
        self.log.save()
        return max_scores
