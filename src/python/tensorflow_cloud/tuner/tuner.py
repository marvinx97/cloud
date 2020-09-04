# Lint as: python3
# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""KerasTuner CloudOracle and CloudTuner classes."""

import datetime
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Text, Union

from kerastuner.engine import hypermodel as hypermodel_module
from kerastuner.engine import hyperparameters as hp_module
from kerastuner.engine import oracle as oracle_module
from kerastuner.engine import trial as trial_module
from kerastuner.engine import tuner as tuner_module
import tensorflow as tf

from tensorflow_cloud.tuner import optimizer_client
from tensorflow_cloud.tuner import utils


class CloudOracle(oracle_module.Oracle):
    """KerasTuner Oracle interface for CAIP Optimizer Service backend."""

    def __init__(
        self,
        project_id: Text,
        region: Text,
        objective: Union[Text, oracle_module.Objective] = None,
        hyperparameters: hp_module.HyperParameters = None,
        study_config: Optional[Dict[Text, Any]] = None,
        max_trials: int = None,
        study_id: Optional[Text] = None,
    ):
        """KerasTuner Oracle interface implmemented with Optimizer backend.

        Args:
            project_id: A GCP project id.
            region: A GCP region. e.g. 'us-central1'.
            objective: If a string, the direction of the optimization (min or
                max) will be inferred.
            hyperparameters: Mandatory and must include definitions for all
                hyperparameters used during the search. Can be used to override
                (or register in advance) hyperparameters in the search space.
            study_config: Study configuration for CAIP Optimizer service.
            max_trials: Total number of trials (model configurations) to test at
                most. If None, it continues the search until it reaches the
                Optimizer trial limit for each study. Users may stop the search
                externally (e.g. by killing the job). Note that the Oracle may
                interrupt the search before `max_trials` models have been
                tested.
            study_id: An identifier of the study. If not supplied,
                system-determined unique ID is given.
                The full study name will be
                `projects/{project_id}/locations/{region}/studies/{study_id}`,
                and the full trial name will be
                `{study name}/trials/{trial_id}`.
        """
        if study_config:
            if objective or hyperparameters:
                raise ValueError(
                    "Please configure either study_config or "
                    '"objective, and hyperparameters".'
                )
            objective = utils.convert_study_config_to_objective(study_config)
            hyperparameters = utils.convert_study_config_to_hps(study_config)
            self.study_config = study_config
        else:
            if not (objective and hyperparameters):
                raise ValueError(
                    "If study_config is not set, "
                    "objective and hyperparameters must be set."
                )
            self.study_config = utils.make_study_config(objective,
                                                        hyperparameters)

        super(CloudOracle, self).__init__(
            objective=objective,
            hyperparameters=hyperparameters,
            max_trials=max_trials,
            allow_new_entries=False,
            tune_new_entries=False,
        )

        if not project_id:
            raise ValueError('"project_id" is not found.')
        self.project_id = project_id

        if not region:
            raise ValueError('"region" is not found.')
        self.region = region

        self.objective = utils.format_objective(objective)
        self.hyperparameters = hyperparameters
        self.max_trials = max_trials

        if study_id:
            self.study_id = "CloudTuner_study_{}".format(study_id)
        else:
            self.study_id = "CloudTuner_study_{}".format(
                datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            )

        self.service = optimizer_client.create_or_load_study(
            self.project_id, self.region, self.study_id, self.study_config
        )

        self.trials = {}
        self._start_time = None

    def create_trial(self, tuner_id: Text) -> trial_module.Trial:
        """Create a new `Trial` to be run by the `Tuner`.

        Args:
            tuner_id: An ID that identifies the `Tuner` requesting a `Trial`.
                `Tuners` that should run the same trial (for instance, when
                running a multi-worker model) should have the same ID. If
                multiple suggestTrialsRequests have the same tuner_id, the
                service will return the identical suggested trial if the trial
                is PENDING, and provide a new trial if the last suggest trial
                was completed.

        Returns:
            A `Trial` object containing a set of hyperparameter values to run
            in a `Tuner`.

        Raises:
            SuggestionInactiveError: Indicates that a suggestion was requested
                from an inactive study.
        """
        # List all trials from the same study and see if any
        # trial.status=STOPPED or if number of trials >= max_limit.
        trial_list = self.service.list_trials()
        # Note that KerasTunerTrialStatus - 'STOPPED' is equivalent to
        # OptimizerTrialState - 'STOPPING'.
        stopping_trials = [t for t in trial_list if t["state"] == "STOPPING"]
        if (self.max_trials and
            len(trial_list) >= self.max_trials) or stopping_trials:
            trial_id = "n"
            hyperparameters = self.hyperparameters.copy()
            hyperparameters.values = None
            # This will break the search loop later.
            return trial_module.Trial(
                hyperparameters=hyperparameters,
                trial_id=trial_id,
                status=trial_module.TrialStatus.STOPPED,
            )

        # Get suggestions
        suggestions = self.service.get_suggestions(tuner_id)

        if "trials" not in suggestions:
            return trial_module.Trial(
                hyperparameters={}, status=trial_module.TrialStatus.STOPPED
            )

        # Fetches the suggested trial.
        # Optimizer Trial instance
        optimizer_trial = suggestions["trials"][0]
        trial_id = utils.get_trial_id(optimizer_trial)

        # KerasTuner Trial instance
        kerastuner_trial = trial_module.Trial(
            hyperparameters=utils.convert_optimizer_trial_to_hps(
                self.hyperparameters.copy(), optimizer_trial
            ),
            trial_id=trial_id,
            status=trial_module.TrialStatus.RUNNING,
        )

        tf.get_logger().info(
            "Hyperparameters requested by tuner ({}): {} ".format(
                tuner_id, kerastuner_trial.hyperparameters.values
            )
        )

        self._start_time = time.time()
        self.trials[trial_id] = kerastuner_trial
        self.ongoing_trials[tuner_id] = kerastuner_trial
        self._save_trial(kerastuner_trial)
        self.save()
        return kerastuner_trial

    def update_trial(self,
                     trial_id: Text,
                     metrics: Mapping[Text, Union[int, float]],
                     step: int = 0):
        """Used by a worker to report the status of a trial."""
        # Constructs the measurement.
        # Adds the measurement of the objective functions to a trial.
        elapsed_secs = time.time() - self._start_time
        if elapsed_secs < 0 or step < 0:
            raise ValueError(
                "Both elapsed_secs and step must be non-negative.")
        if elapsed_secs == 0 and step == 0:
            raise ValueError(
                "At least one of {elapsed_secs, step} must be positive")

        metric_list = []
        for ob in self.objective:
            if ob.name not in metrics:
                tf.get_logger().info(
                    'Objective "{}" is not found in metrics.'.format(ob.name)
                )
                continue
            metric_list.append(
                {"metric": ob.name, "value": float(metrics.get(ob.name))}
            )

        self.service.report_intermediate_objective_value(
            step, elapsed_secs, metric_list, trial_id
        )

        kerastuner_trial = self.trials[trial_id]

        # Checks whether a trial should stop or not.
        tf.get_logger().info("UpdateTrial: polls the stop decision.")
        should_stop = self.service.should_trial_stop(trial_id)

        if should_stop:
            kerastuner_trial.status = trial_module.TrialStatus.STOPPED
        return kerastuner_trial.status

    def end_trial(self, trial_id: Text, status: Text = "COMPLETED"):
        """Record the measured objective for a set of parameter values."""
        kerastuner_trial = None
        for tuner_id, ongoing_trial in self.ongoing_trials.items():
            if ongoing_trial.trial_id == trial_id:
                tf.get_logger().info(
                    "End trial requested by tuner ({})".format(tuner_id)
                )
                kerastuner_trial = self.ongoing_trials.pop(tuner_id)
                break

        if not kerastuner_trial:
            raise ValueError(
                "Ongoing trial with id: {} not found.".format(trial_id))

        kerastuner_trial.status = status
        if status == trial_module.TrialStatus.COMPLETED:
            trial_infeasible = False
            infeasibility_reason = None
        elif status == trial_module.TrialStatus.INVALID:
            trial_infeasible = True
            infeasibility_reason = status
        else:
            raise ValueError(
                'Unexpected status passed. Expected "COMPLETED" or '
                '"INVALID", found {}'.format(status)
            )

        optimizer_trial = self.service.complete_trial(
            trial_id, trial_infeasible, infeasibility_reason
        )

        if status == trial_module.TrialStatus.COMPLETED:
            final_measurement = optimizer_trial["finalMeasurement"]
            # If epoch = 1, set the best_step = 1.
            kerastuner_trial.best_step = final_measurement.get("stepCount", 1)
            kerastuner_trial.score = final_measurement["metrics"][0]["value"]
        self._save_trial(kerastuner_trial)
        self.save()

    def get_best_trials(self, num_trials: int = 1) -> List[trial_module.Trial]:
        """Returns the trials with the best objective values found so far.

        Arguments:
            num_trials: positive int, number of trials to return.
        Returns:
            List of KerasTuner Trials.
        """
        if len(self.objective) > 1:
            raise ValueError(
                "Getting the best trials for multi-objective optimization "
                "is not supported. "
            )

        maximizing = (
            utils.format_goal(self.objective[0].direction) == "MAXIMIZE")

        # List all trials associated with the same study
        trial_list = self.service.list_trials()

        optimizer_trials = [t for t in trial_list if t["state"] == "COMPLETED"]

        if not optimizer_trials:
            return []

        sorted_trials = sorted(
            optimizer_trials,
            key=lambda t: t["finalMeasurement"]["metrics"][0]["value"],
            reverse=maximizing,
        )
        best_optimizer_trials = sorted_trials[:num_trials]

        best_trials = []
        # Convert Optimizer trials to KerasTuner Trial instance
        for optimizer_trial in best_optimizer_trials:
            final_measurement = optimizer_trial["finalMeasurement"]
            kerastuner_trial = trial_module.Trial(
                hyperparameters=utils.convert_optimizer_trial_to_hps(
                    self.hyperparameters.copy(), optimizer_trial
                ),
                trial_id=utils.get_trial_id(optimizer_trial),
                status=trial_module.TrialStatus.COMPLETED,
            )
            # If trial had ended before having intermediate metric reporting,
            # set epoch = 1.
            kerastuner_trial.best_step = final_measurement.get("stepCount", 1)
            kerastuner_trial.score = final_measurement["metrics"][0]["value"]
            best_trials.append(kerastuner_trial)
        return best_trials


class CloudTuner(tuner_module.Tuner):
    """KerasTuner interface implementation backed by CAIP Optimizer Service."""

    def __init__(
        self,
        hypermodel: Union[hypermodel_module.HyperModel,
                          Callable[[hp_module.HyperParameters],
                                   tf.keras.Model]],
        project_id: Text,
        region: Text,
        objective: Union[Text, oracle_module.Objective] = None,
        hyperparameters: hp_module.HyperParameters = None,
        study_config: Optional[Dict[Text, Any]] = None,
        max_trials: int = None,
        study_id: Optional[Text] = None,
        train_locally: Optional[bool] = True,
        container_uri: Optional[Text] = None,
        staging_bucket: Optional[Text] = None,
        job_spec: Optional[Text] = None,
        **kwargs
    ):

        """Constructor.

        Args:
            hypermodel: Instance of HyperModel class (or callable that takes
                hyperparameters and returns a Model instance).
            project_id: A GCP project id.
            region: A GCP region. e.g. 'us-central1'.
            objective: Name of model metric to minimize or maximize, e.g.
                "val_accuracy".
            hyperparameters: Can be used to override (or register in advance)
                hyperparamters in the search space.
            study_config: Study configuration for CAIP Optimizer service.
            max_trials: Total number of trials (model configurations) to test at
                most. Note that the oracle may interrupt the search before
                `max_trials` models have been tested if the search space has
                been exhausted.
            study_id: An identifier of the study. The full study name will be
                projects/{project_id}/locations/{region}/studies/{study_id}.
            train_locally: A flag to specify execution environment. Set to True
                to train locally, and False to use AI Platform training.
            container_uri: based image to use for AI Platform Training. If not
                specified will use the latest AI Platform Tensorflow deep
                learning container.
            staging_bucket: Google Cloud Storage path for temporary assets and
                AI Platform training output, required for remote training.
                Overwrites value provided in job spec.
            job_spec: AI Platform Training job_spec. If none is provided a
                default cluster spec and distribution strategy will be used.
            **kwargs: Keyword arguments relevant to all `Tuner` subclasses.
                Please see the docstring for `Tuner`.
        """
        self.train_locally = train_locally
        self.container_uri = container_uri
        self.staging_bucket = staging_bucket
        self.job_spec = job_spec

        oracle = CloudOracle(
            project_id=project_id,
            region=region,
            objective=objective,
            hyperparameters=hyperparameters,
            study_config=study_config,
            max_trials=max_trials,
            study_id=study_id,
        )
        super(CloudTuner, self).__init__(
            oracle=oracle, hypermodel=hypermodel, **kwargs
        )

    def run_trial(self, trial, *fit_args, **fit_kwargs):
        """Evaluates a set of hyperparameter values.

        This method is called during `search` to evaluate a set of
        hyperparameters.
        Arguments:
            trial: A `Trial` instance that contains the information
              needed to run this trial. `Hyperparameters` can be accessed
              via `trial.hyperparameters`.
            *fit_args: Positional arguments passed by `search`.
            **fit_kwargs: Keyword arguments passed by `search`.
        """
        if self.train_locally:
            super(CloudTuner, self).run_trial(trial, *fit_args, **fit_kwargs)
            return
        raise NotImplementedError("Remote execution is not implemented.")

    def save_model(self, trial_id, model, step=0):
        if self.train_locally:
            super(CloudTuner, self).save_model(trial_id, model, step=0)
            return
        raise NotImplementedError("Remote execution is not implemented.")

    def load_model(self, trial):
        if self.train_locally:
            return super(CloudTuner, self).load_model(trial)
        raise NotImplementedError("Remote execution is not implemented.")

    def on_epoch_end(self, trial, model, epoch, logs=None):
        """A hook called at the end of every epoch.

        Arguments:
            trial: A `Trial` instance.
            model: A Keras `Model`.
            epoch: The current epoch number.
            logs: Dict. Metrics for this epoch. This should include
              the value of the objective for this epoch.
        """
        if self.train_locally:
            super(CloudTuner, self).on_epoch_end(trial, model, epoch, logs=None)
            return

        raise NotImplementedError("Remote execution is not implemented.")

    def _configure_tensorboard_dir(self, callbacks, trial_id):
        if self.train_locally:
            return super(
                CloudTuner,
                self
                )._configure_tensorboard_dir(callbacks, trial_id)
        raise NotImplementedError("Remote execution is not implemented.")

    def _get_checkpoint_dir(self, trial_id, epoch):
        if self.train_locally:
            return super(CloudTuner, self)._get_checkpoint_dir(trial_id, epoch)
        raise NotImplementedError("Remote execution is not implemented.")

    def _get_checkpoint_fname(self, trial_id, epoch):
        if self.train_locally:
            return super(
                CloudTuner,
                self
                )._get_checkpoint_fname(trial_id, epoch)

        raise NotImplementedError("Remote execution is not implemented.")

    def _checkpoint_model(self, model, trial_id, epoch):
        if self.train_locally:
            return super(
                CloudTuner,
                self
                )._checkpoint_model(model, trial_id, epoch)
        raise NotImplementedError("Remote execution is not implemented.")
