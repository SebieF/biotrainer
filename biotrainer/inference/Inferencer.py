import torch

from typing import Union, Optional, Dict, Iterable, List

from torch.utils.data import DataLoader

from ..utilities import get_device
from ..losses import get_loss
from ..models import get_model
from ..optimizers import get_optimizer
from ..solvers import get_solver
from ..datasets import get_dataset, get_collate_function


class Inferencer:

    def __init__(
          self,

          # These are from the original input config
          protocol: str,
          n_classes: int,
          n_features: int,
          model_choice: str,
          embedder_name: str,

          # These are from post-training
          log_dir: str,
          class_int_to_string: Optional[Dict[int, str]],

          # Fillers
          learning_rate: float = 1e-3,
          loss_choice: str = "cross_entropy_loss",
          optimizer_choice: str = "adam", batch_size: int = 128,
          device: Union[None, str, torch.device] = None, embeddings_file_path: str = None,

          # Everything else
          **kwargs
    ):

        model = get_model(
            protocol=protocol, model_choice=model_choice,
            n_classes=n_classes, n_features=n_features
        )
        loss_function = get_loss(
            protocol=protocol, loss_choice=loss_choice
        )
        optimizer = get_optimizer(
            protocol=protocol, optimizer_choice=optimizer_choice,
            learning_rate=learning_rate, model_parameters=model.parameters()
        )

        self.device = get_device(device)
        self.batch_size = batch_size
        self.class_int_to_string = class_int_to_string
        self.protocol = protocol
        self.embedder_name = embedder_name

        self.solver = get_solver(
            protocol, network=model, optimizer=optimizer, loss_function=loss_function, device=self.device,
            experiment_dir=log_dir
        )
        self.collate_function = get_collate_function(protocol)
        self.solver.load_checkpoint()

    def from_embeddings(self, embeddings: Iterable) -> List[Union[str, int]]:
        dataset = get_dataset(self.protocol, samples={
            idx: (torch.tensor(embedding), torch.zeros(torch.tensor(embedding).shape[0]))
            for idx, embedding in enumerate(embeddings)
        })

        dataloader = DataLoader(
            dataset=dataset, batch_size=self.batch_size, shuffle=False, drop_last=False,
            collate_fn=self.collate_function
        )

        results = self.solver.inference(dataloader)

        if self.protocol == 'residue_to_class':
            results['predictions'] = ["".join(
                [self.class_int_to_string[p] for p in prediction]
            ) for prediction in results['predictions']]
        # If sequence-to-class problem, map the integers back to the class labels (whatever length)
        elif self.protocol == "sequence_to_class":
            results['predictions'] = [self.class_int_to_string[p] for p in results['predictions']]

        return results['predictions']
