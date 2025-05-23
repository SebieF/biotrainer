import time
import torch
import random
import datetime

from pathlib import Path
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from typing import Optional, Dict, Any, Union, List

from .target_manager import TargetManager
from .hp_manager import HyperParameterManager
from .cv_splitter import CrossValidationSplitter

from ..losses import get_loss
from ..protocols import Protocol
from ..inference import Inferencer
from ..optimizers import get_optimizer
from ..validations import SanityChecker, Bootstrapper
from ..solvers import get_solver, Solver, get_metrics_calculator
from ..models import count_parameters, get_model
from ..datasets import get_collate_function, get_dataset
from ..embedders import EmbeddingService, get_embedding_service
from ..utilities import seed_all, Split, SplitResult, DatasetSample, METRICS_WITHOUT_REVERSED_SORTING, __version__, \
    revert_mappings, get_logger

logger = get_logger(__name__)


class Trainer:
    _class_weights: Union[None, torch.FloatTensor] = None

    def __init__(self,
                 # Needed
                 sequence_file: str,
                 protocol: Protocol,
                 output_dir: str,
                 hp_manager: HyperParameterManager,
                 output_vars: Dict[str, Any],
                 # Optional with defaults
                 labels_file: Optional[str] = None, mask_file: Optional[str] = None,
                 embedder_name: str = "custom_embeddings",
                 custom_tokenizer_config: Optional[str] = None,
                 use_half_precision: bool = False,
                 embeddings_file: str = None,
                 seed: int = 42,
                 device: torch.device = None,
                 auto_resume: bool = False,
                 pretrained_model: str = None,
                 save_split_ids: bool = False,
                 ignore_file_inconsistencies: bool = False,
                 external_writer: Optional[str] = None,
                 limited_sample_size: int = -1,
                 cross_validation_config: Dict[str, Any] = None,
                 interaction: Optional[str] = None,
                 sanity_check: bool = True,
                 dimension_reduction_method: Optional[str] = None,
                 n_reduced_components: Optional[int] = None,
                 bootstrapping_iterations: int = 30,
                 # Ignore rest
                 **kwargs
                 ):
        self._output_vars = output_vars

        self._sequence_file = sequence_file
        self._protocol = protocol
        self._output_dir = Path(output_dir)
        self._labels_file = labels_file
        self._mask_file = mask_file
        self._embedder_name = embedder_name
        self._custom_tokenizer_config = custom_tokenizer_config
        self._use_half_precision = use_half_precision
        self._embeddings_file = embeddings_file
        self._seed = seed
        self._device = device
        self._auto_resume = auto_resume
        self._pretrained_model = Path(pretrained_model) if pretrained_model is not None else None
        self._save_split_ids = save_split_ids
        self._ignore_file_inconsistencies = ignore_file_inconsistencies
        self._external_writer = external_writer
        self._interaction = interaction
        self._limited_sample_size = limited_sample_size
        self._cross_validation_config = cross_validation_config
        self._cross_validation_splitter = CrossValidationSplitter(self._protocol, self._cross_validation_config)
        self._hp_manager = hp_manager
        self._sanity_check = sanity_check
        self._dimension_reduction_method = dimension_reduction_method
        self._n_reduced_components = n_reduced_components
        self._bootstrapping_iterations = bootstrapping_iterations

    def training_and_evaluation_routine(self):
        # SETUP
        self._setup()

        # EMBEDDINGS
        id2emb = self._create_and_load_embeddings()

        # TARGETS => DATASETS
        target_manager = TargetManager(protocol=self._protocol, sequence_file=self._sequence_file,
                                       labels_file=self._labels_file, mask_file=self._mask_file,
                                       ignore_file_inconsistencies=self._ignore_file_inconsistencies,
                                       cross_validation_method=self._cross_validation_config["method"],
                                       interaction=self._interaction)
        train_dataset, val_dataset, test_datasets, prediction_dataset = target_manager.get_datasets_by_annotations(
            id2emb)
        del id2emb  # No longer required and should not be used later in the routine

        # LOG COMMON VALUES FOR ALL k-fold SPLITS:
        embeddings_dimension = train_dataset[0].embedding.shape[-1]  # Last position in shape is always embedding dim
        logger.info(f"Number of features: {embeddings_dimension}")
        self._output_vars['n_features'] = embeddings_dimension
        self._output_vars['n_testing_ids'] = sum(len(test_dataset) for test_dataset in test_datasets.values())
        self._output_vars['n_classes'] = target_manager.number_of_outputs

        # CLASS WEIGHTS
        self._class_weights = self._get_class_weights(target_manager=target_manager)

        # CREATE SPLITS:
        splits = self._cross_validation_splitter.split(train_dataset=train_dataset, val_dataset=val_dataset)

        # RUN CROSS VALIDATION
        self._output_vars["split_results"] = {}
        nested_cv = False
        if "nested" in self._cross_validation_config.keys():
            nested_cv = eval(str(self._cross_validation_config["nested"]).capitalize())

        start_time_total = time.perf_counter()
        if nested_cv:
            split_results = self._run_nested_cross_validation(splits)
        else:
            split_results = self._run_cross_validation(splits)
        end_time_total = time.perf_counter()
        self._output_vars["elapsed_time_total"] = end_time_total - start_time_total

        self._log_average_result_of_splits(split_results)
        best_split = self._get_best_model_of_splits(split_results)

        # TESTING
        for test_set_id, test_dataset in test_datasets.items():
            logger.info('Running final evaluation on the best model')
            test_dataset_embeddings = self._create_embeddings_dataset(test_dataset, mode="test")
            test_loader = self._create_dataloader(dataset=test_dataset_embeddings, hyper_params=best_split.hyper_params)
            test_results = self._do_and_log_evaluation(solver=best_split.solver,
                                                       test_loader=test_loader,
                                                       target_manager=target_manager,
                                                       test_set_id=test_set_id)

            # ADDITIONAL EVALUATION
            metrics_calculator = get_metrics_calculator(protocol=self._protocol,
                                                        device=self._device,
                                                        num_classes=self._output_vars.get('n_classes', 0))
            # BOOTSTRAPPING
            if self._bootstrapping_iterations > 0:
                self._do_and_log_bootstrapping_evaluation(metrics_calculator=metrics_calculator,
                                                          test_results=test_results,
                                                          test_loader=test_loader,
                                                          test_set_id=test_set_id)

            # SANITY CHECKER
            if self._sanity_check:
                sanity_checker = SanityChecker(output_vars=self._output_vars,
                                               train_val_dataset=train_dataset + val_dataset,
                                               test_dataset=test_dataset,
                                               test_loader=test_loader,
                                               metrics_calculator=metrics_calculator,
                                               test_results_dict=test_results,
                                               mode="warn")
                baseline_results = sanity_checker.check_test_results(test_set_id=test_set_id)
                if baseline_results is not None and len(baseline_results) > 0:
                    self._output_vars["test_iterations_results"][test_set_id].update(
                        {"test_baselines": baseline_results}
                    )

        # PREDICTION
        if len(prediction_dataset) > 0:
            logger.info(f'Calculating predictions for {len(prediction_dataset)} samples!')
            pred_dataset_embeddings = self._create_embeddings_dataset(prediction_dataset, mode="pred")
            pred_loader = self._create_dataloader(dataset=pred_dataset_embeddings, hyper_params=best_split.hyper_params)

            _ = self._do_and_log_prediction(solver=best_split.solver,
                                            pred_loader=pred_loader,
                                            target_manager=target_manager)

        # SAVE BEST SPLIT AS ONNX
        try:
            best_split.solver.save_as_onnx(embedding_dimension=embeddings_dimension)
        except Exception as e:
            logger.error("Could not save model as ONNX!")

        # CONVERT PROTOCOL CLASS TO STRING
        self._output_vars["protocol"] = self._output_vars["protocol"].name

        logger.info(f"Extensive output information can be found at {self._output_vars['output_dir']}/out.yml")
        return self._output_vars

    def _setup(self):
        # Log model hash
        logger.info(f"Training model with hash: {self._output_vars['model_hash']}")
        # Seed
        seed_all(self._seed)
        # Log device
        self._output_vars['device'] = str(self._device)
        logger.info(f"Using device: {self._device}")
        # Log version
        self._output_vars['biotrainer_version'] = str(__version__)

    def _create_and_load_embeddings(self) -> Dict[str, Any]:
        # Generate embeddings if necessary, otherwise use existing embeddings and overwrite embedder_name
        embeddings_file = self._embeddings_file
        embedding_service: EmbeddingService = get_embedding_service(embeddings_file_path=embeddings_file,
                                                                    custom_tokenizer_config=self._custom_tokenizer_config,
                                                                    embedder_name=self._embedder_name,
                                                                    use_half_precision=self._use_half_precision,
                                                                    device=self._device)
        if not embeddings_file or not Path(embeddings_file).is_file():
            embeddings_file = embedding_service.compute_embeddings(
                input_data=self._sequence_file,
                protocol=self._protocol, output_dir=self._output_dir
            )
            # Add to out config
            self._output_vars['embeddings_file'] = embeddings_file
        else:
            logger.info(f'Embeddings file was found at {embeddings_file}. Embeddings have not been computed.')
            self._embedder_name = f"precomputed_{Path(embeddings_file).stem}_{self._embedder_name}"

        # Mapping from id to embeddings
        id2emb = embedding_service.load_embeddings(embeddings_file_path=embeddings_file)
        if self._is_dimension_reduction_possible(id2emb):
            id2emb = embedding_service.embeddings_dimensionality_reduction(
                embeddings=id2emb,
                dimension_reduction_method=self._dimension_reduction_method,
                n_reduced_components=self._n_reduced_components)
        return id2emb

    def _is_dimension_reduction_possible(self, embeddings: Dict[str, Any]) -> bool:
        if (self._protocol.using_per_sequence_embeddings() and
                self._dimension_reduction_method and
                self._n_reduced_components and
                len(embeddings) >= 3 and
                list(embeddings.values())[0].shape[0] >= 3):
            return True
        else:
            if (self._dimension_reduction_method and
                    self._n_reduced_components):
                if len(embeddings) < 3:
                    raise Exception(f"Dimensionality reduction cannot be performed as \
                                the number of samples is less than 3")
                if list(embeddings.values())[0].shape[0] < 3:
                    raise Exception(f"Dimensionality reduction cannot be performed as \
                                the original embedding dimension is less than 3")
                if not self._protocol.using_per_sequence_embeddings():
                    raise Exception(f"Dimensionality reduction cannot be performed as \
                                the embeddings are not per-protein embeddings")
            return False

    def _get_class_weights(self, target_manager: TargetManager) -> Union[None, torch.FloatTensor]:
        # Get x_to_class specific logs and weights
        class_weights = None
        if self._protocol in Protocol.classification_protocols():
            self._output_vars['class_int_to_string'] = target_manager.class_int2str
            self._output_vars['class_str_to_int'] = target_manager.class_str2int
            logger.info(f"Number of classes: {self._output_vars['n_classes']}")
            # Compute class weights to pass as bias to model if option is set
            class_weights = target_manager.compute_class_weights()
            if class_weights is not None:
                self._output_vars['computed_class_weights'] = {class_index: class_value.item() for
                                                               class_index, class_value in enumerate(class_weights)}

        return class_weights

    def _create_embeddings_dataset(self, split: List[DatasetSample], mode: str):
        # Apply limited sample number
        if mode == "train" and self._limited_sample_size > 0:
            logger.info(f"Using limited sample size of {self._limited_sample_size} for training dataset")
            split = random.sample(split,
                                  k=min(self._limited_sample_size, len(split)))
        return get_dataset(self._protocol, split)

    def _create_dataloader(self, dataset, hyper_params: Dict) -> torch.utils.data.dataloader.DataLoader:
        # Create dataloader from dataset
        return DataLoader(
            dataset=dataset, batch_size=hyper_params["batch_size"], shuffle=hyper_params["shuffle"], drop_last=False,
            collate_fn=get_collate_function(self._protocol)
        )

    def _create_writer(self, hyper_params: Dict) -> torch.utils.tensorboard.writer.SummaryWriter:
        if self._external_writer == "tensorboard":
            # Tensorboard writer
            writer = SummaryWriter(log_dir=str(self._output_dir / "runs"))

            writer.add_hparams({
                'model': hyper_params["model_choice"],
                'num_epochs': hyper_params["num_epochs"],
                'use_class_weights': hyper_params["use_class_weights"],
                'learning_rate': hyper_params["learning_rate"],
                'batch_size': hyper_params["batch_size"],
                'embedder_name': self._embedder_name,
                'seed': self._seed,
                'loss': hyper_params["loss_choice"],
                'optimizer': hyper_params["optimizer_choice"],
            }, {})

            return writer
        return None

    def _create_solver(self, split_name, model, loss_function, optimizer, writer, hyper_params: Dict) -> Solver:
        return get_solver(protocol=self._protocol, name=split_name, network=model, optimizer=optimizer,
                          loss_function=loss_function, device=self._device, number_of_epochs=hyper_params["num_epochs"],
                          patience=hyper_params["patience"], epsilon=hyper_params["epsilon"], log_writer=writer,
                          log_dir=hyper_params["log_dir"], num_classes=self._output_vars['n_classes'])

    def _run_cross_validation(self, splits: List[Split]) -> List[SplitResult]:
        split_results = list()
        for split in splits:
            for hyper_params in self._hp_manager.search(mode="no_search"):
                logger.info(f"Training model for split {split.name}:")
                best_epoch_metrics, solver = self._do_training_by_split(outer_split=split,
                                                                        hyper_params=hyper_params)
                split_results.append(SplitResult(split.name, hyper_params, best_epoch_metrics, solver))

        return split_results

    def _run_nested_cross_validation(self, splits: List[Split]) -> List[SplitResult]:
        hp_search_method = self._cross_validation_config["search_method"]
        split_results_outer = list()
        for outer_k, outer_split in enumerate(splits):
            hyper_param_metric_results = list()
            for hp_iteration, hyper_params in enumerate(self._hp_manager.search(mode=hp_search_method)):
                inner_splits = self._cross_validation_splitter.nested_split(train_dataset=outer_split.train,
                                                                            current_outer_k=outer_k + 1,
                                                                            hp_iteration=hp_iteration + 1
                                                                            )
                split_results_inner = list()
                for inner_split in inner_splits:
                    logger.info(f"Training model for inner split {inner_split.name}:")
                    best_epoch_metrics_inner, s_inner = self._do_training_by_split(outer_split=outer_split,
                                                                                   hyper_params=hyper_params,
                                                                                   inner_split=inner_split)
                    split_results_inner.append(
                        SplitResult(inner_split.name, hyper_params, best_epoch_metrics_inner, s_inner))

                hyper_param_metric_results.append((hyper_params,
                                                   self._get_average_of_chosen_metric_for_splits(split_results_inner)
                                                   ))

            # SELECT BEST HYPER PARAMETER COMBINATION BY AVERAGE METRIC TO OPTIMIZE
            hyper_param_metric_results = self._sort_according_to_chosen_metric(
                list_to_sort=hyper_param_metric_results,
                key=lambda hp_metric_res: hp_metric_res[1]
            )

            best_hyper_param_combination = hyper_param_metric_results[0][0]
            # TRAIN ON split.train, VALIDATE ON split.val
            logger.info(f"Training model for outer split {outer_split.name} with best hyper_parameter combination "
                        f"{self._hp_manager.get_only_params_to_optimize(best_hyper_param_combination)} "
                        f"(criterion: {self._cross_validation_config['choose_by']}):")
            best_epoch_metrics, solver = self._do_training_by_split(outer_split=outer_split,
                                                                    hyper_params=best_hyper_param_combination)
            split_results_outer.append(
                SplitResult(outer_split.name, best_hyper_param_combination, best_epoch_metrics, solver))

        return split_results_outer

    def _do_training_by_split(self, outer_split: Split, hyper_params: Dict[str, Any], inner_split: Split = None):
        # Necessary for differentiation of splits during nested k-fold cross validation
        current_split = inner_split if inner_split else outer_split

        # SETUP SAVE DICT
        if outer_split.name not in self._output_vars["split_results"].keys():
            self._output_vars["split_results"][outer_split.name] = {}

        if inner_split:
            self._output_vars["split_results"][outer_split.name][inner_split.name] = {}
            save_dict = self._output_vars["split_results"][outer_split.name][inner_split.name]
        else:
            save_dict = self._output_vars["split_results"][outer_split.name]

        # DATASETS
        train_dataset = self._create_embeddings_dataset(current_split.train, mode="train")
        val_dataset = self._create_embeddings_dataset(current_split.val, mode="val")

        save_dict['n_training_ids'] = len(train_dataset)
        save_dict['n_validation_ids'] = len(val_dataset)
        save_dict['split_hyper_params'] = self._hp_manager.get_only_params_to_optimize(hyper_params)
        if self._save_split_ids:
            save_dict['training_ids'] = [sample.seq_id for sample in current_split.train]
            save_dict['validation_ids'] = [sample.seq_id for sample in current_split.val]

        # DATALOADERS
        train_loader = self._create_dataloader(dataset=train_dataset, hyper_params=hyper_params)
        val_loader = self._create_dataloader(dataset=val_dataset, hyper_params=hyper_params)

        # MODEL, LOSS, OPTIMIZER
        model, loss_function, optimizer = self._create_model_loss_optimizer(
            class_weights=self._class_weights if hyper_params["use_class_weights"] else None,
            **hyper_params)

        # Count and log number of free params
        n_free_parameters = count_parameters(model)
        save_dict['n_free_parameters'] = n_free_parameters

        # WRITER
        writer = self._create_writer(hyper_params=hyper_params)

        # SOLVER
        solver = self._create_solver(split_name=current_split.name,
                                     model=model, loss_function=loss_function, optimizer=optimizer, writer=writer,
                                     hyper_params=hyper_params)
        # TRAINING/VALIDATION
        if self._auto_resume:
            best_epoch_metrics = solver.auto_resume(training_dataloader=train_loader, validation_dataloader=val_loader,
                                                    train_wrapper=lambda untrained_solver: self._do_and_log_training(
                                                        split_name=current_split.name,
                                                        solver=untrained_solver,
                                                        train_loader=train_loader,
                                                        val_loader=val_loader,
                                                        save_dict=save_dict
                                                    )
                                                    )
        else:
            if self._pretrained_model:
                solver.load_checkpoint(checkpoint_path=self._pretrained_model, resume_training=True)
            best_epoch_metrics = self._do_and_log_training(split_name=current_split.name,
                                                           solver=solver,
                                                           train_loader=train_loader,
                                                           val_loader=val_loader,
                                                           save_dict=save_dict)

        # Save metrics from best training epoch
        save_dict['training_iteration_result_best_epoch'] = best_epoch_metrics

        return best_epoch_metrics, solver

    def _create_model_loss_optimizer(self, class_weights: Optional[torch.Tensor] = None,
                                     **kwargs) -> (torch.nn.Module, torch.nn.Module, torch.nn.Module):
        # Initialize model
        model = get_model(n_classes=self._output_vars["n_classes"], n_features=self._output_vars["n_features"],
                          **kwargs)

        # Initialize loss function
        loss_function = get_loss(weight=class_weights, **kwargs)

        # Initialize optimizer
        optimizer = get_optimizer(model_parameters=model.parameters(), **kwargs)

        return model, loss_function, optimizer

    @staticmethod
    def _do_and_log_training(split_name: str, solver, train_loader, val_loader, save_dict: Dict):
        start_time_abs = datetime.datetime.now()
        start_time = time.perf_counter()
        epoch_iterations = solver.train(train_loader, val_loader)
        end_time = time.perf_counter()
        end_time_abs = datetime.datetime.now()

        # Logging
        logger.info(f'Total training time for {split_name}: {(end_time - start_time) / 60:.1f}[m]')

        # Save training time for prosperity
        save_dict['start_time'] = start_time_abs
        save_dict['end_time'] = end_time_abs
        save_dict['elapsed_time'] = end_time - start_time

        return epoch_iterations[solver.get_best_epoch()]

    def _sort_according_to_chosen_metric(self, list_to_sort: List, key):
        choose_by_metric = self._cross_validation_config["choose_by"]
        reverse = choose_by_metric not in METRICS_WITHOUT_REVERSED_SORTING
        return sorted(list_to_sort,
                      key=key,
                      reverse=reverse)

    def _get_best_model_of_splits(self, split_results: List[SplitResult]) -> SplitResult:
        choose_by_metric = self._cross_validation_config["choose_by"]
        split_results_sorted = self._sort_according_to_chosen_metric(split_results,
                                                                     lambda split_result:
                                                                     split_result.best_epoch_metrics["validation"][
                                                                         choose_by_metric])
        best_split_result = split_results_sorted[0]
        if len(split_results) > 1:  # Not for hold_out cross validation
            logger.info(f"Using best model from split {best_split_result.name} "
                        f"(criterion: {self._cross_validation_config['choose_by']}) for test set evaluation")
            self._output_vars["split_results"]["best_split"] = best_split_result.name
        return best_split_result

    def _get_average_of_chosen_metric_for_splits(self, split_results: List[SplitResult]) -> float:
        choose_by_metric = self._cross_validation_config["choose_by"]
        sum_metric = sum([split_result.best_epoch_metrics["validation"][choose_by_metric]
                          for split_result in split_results])
        return sum_metric / len(split_results)

    def _log_average_result_of_splits(self, split_results: List[SplitResult]):
        n = len(split_results)
        if n > 1:  # Not for hold_out cross validation
            average_dict = {}
            result_metric_keys = split_results[0].best_epoch_metrics["validation"].keys()
            for key in result_metric_keys:
                average_dict[key] = sum(
                    [split_result.best_epoch_metrics["validation"][key] for split_result in split_results]) / n
            logger.info(f"Average split results: {average_dict}")
            self._output_vars["split_results"]["average_outer_split_results"] = average_dict

    def _do_and_log_evaluation(self, solver, test_loader, target_manager, test_set_id: str):
        # re-initialize the model to avoid any undesired information leakage and only load checkpoint weights
        solver.load_checkpoint(resume_training=False)
        test_results = solver.inference(test_loader, calculate_test_metrics=True)

        if 'test_iterations_results' not in self._output_vars:
            self._output_vars['test_iterations_results'] = {}
        if self._save_split_ids:
            test_results['test_set_predictions'] = revert_mappings(protocol=self._protocol,
                                                                   test_predictions=test_results['mapped_predictions'],
                                                                   class_int2str=target_manager.class_int2str)
            self._output_vars['test_iterations_results'][test_set_id] = {k: v for k, v in test_results.items()
                                                                         if k != "mapped_probabilities"}
        else:
            self._output_vars['test_iterations_results'][test_set_id] = {'metrics': test_results['metrics']}

        logger.info(f"Test set {test_set_id} metrics: {test_results['metrics']}")
        return test_results

    def _do_and_log_prediction(self, solver, pred_loader, target_manager):
        # re-initialize the model to avoid any undesired information leakage and only load checkpoint weights
        solver.load_checkpoint(resume_training=False)
        pred_results = solver.inference(pred_loader, calculate_test_metrics=False)

        self._output_vars['predictions'] = revert_mappings(protocol=self._protocol,
                                                           test_predictions=pred_results['mapped_predictions'],
                                                           class_int2str=target_manager.class_int2str)

        logger.info(f"Calculated predictions for {len(pred_loader)} samples!")
        return pred_results

    def _do_and_log_bootstrapping_evaluation(self, metrics_calculator, test_results, test_loader, test_set_id: str):
        logger.info(f'Running bootstrapping evaluation on the best model for test set ({test_set_id})')
        bootstrapping_dict = Bootstrapper.bootstrap(protocol=self._protocol, device=self._device,
                                                    bootstrapping_iterations=self._bootstrapping_iterations,
                                                    metrics_calculator=metrics_calculator,
                                                    mapped_predictions=test_results["mapped_predictions"],
                                                    test_loader=test_loader)
        bootstrapping_results = bootstrapping_dict["results"]
        self._output_vars['test_iterations_results'][test_set_id].update({"bootstrapping": bootstrapping_results})
        logger.info(f'Bootstrapping results for test set ({test_set_id}): {bootstrapping_dict}')
