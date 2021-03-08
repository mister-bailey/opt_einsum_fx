from typing import Callable, Union
import warnings

import torch
from torch import fx
from torch.fx.passes.shape_prop import ShapeProp

import opt_einsum
from opt_einsum.contract import _core_contract


_EINSUM_FUNCS = {torch.functional.einsum, torch.einsum}


def optimize_einsums(
    model: Union[torch.nn.Module, Callable],
    example_inputs: tuple,
    tracer_class: type = fx.Tracer,
) -> torch.nn.Module:
    """Optimize einsums in ``model`` for ``example_inputs``.

    All of the restrictions of ``torch.fx`` symbolic tracing apply.


    Args:
        model (torch.nn.Module or callable): the model or function to optimize
        example_inputs (tuple): arguments to ``model`` whose shapes will determine the einsum optimizations.
        tracer_class (type, optional): the tracer class to use to turn ``model`` into an ``fx.Graph``.
    """
    if isinstance(model, fx.GraphModule):
        graph: fx.Graph = model.graph
    else:
        tracer: fx.Tracer = tracer_class()
        graph: fx.Graph = tracer.trace(model)
        model = tracer.root
    out_mod = fx.GraphModule(model, graph)
    # shapeprop
    sp = ShapeProp(out_mod)
    sp.run(*example_inputs)
    out_mod.graph = optimize_einsums_graph(out_mod.graph)
    out_mod.recompile()
    return out_mod


# Based on "Proxy Retracing" example in https://pytorch.org/docs/stable/fx.html
def optimize_einsums_graph(graph: fx.Graph) -> fx.Graph:
    """Optimize einsums in a ``torch.fx.Graph``.

    ``graph`` must have shape information such as that populated by ``torch.fx.passes.shape_prop.ShapeProp``.
    """
    new_graph = fx.Graph()
    # env keeps track of new injected nodes in addition to existing ones,
    # making sure they get into new_graph
    env = {}
    node_processed: bool = False
    for node in graph.nodes:
        node_processed = False
        if node.op == "call_function" and node.target in _EINSUM_FUNCS:
            # Get shapes:
            try:
                shapes = [a.shape for a in node.args[1:]]
            except AttributeError:
                warnings.warn(
                    f"einsum {repr(node)} lacked shape information; "
                    "not optimizing. "
                    "Did you forget to run ShapeProp on this graph?",
                    RuntimeWarning,
                )
            else:
                # We have shapes, so:
                # Determine the optimal contraction
                path, path_info = opt_einsum.contract_path(
                    node.args[0], *shapes, shapes=True  # the einstr
                )
                # By wrapping the arguments with proxies,
                # we can dispatch to opt_einsum and implicitly
                # add it to the Graph by symbolically tracing it.
                proxy_args = [
                    fx.Proxy(env[x.name]) if isinstance(x, fx.Node) else x
                    for x in node.args
                ]
                # Use _core_contract to avoid `len()` calls that
                # fx can't deal with
                output_proxy = _core_contract(
                    proxy_args[1:],
                    path_info.contraction_list,
                    backend="torch",
                    evaluate_constants=False,
                )

                # Operations on `Proxy` always yield new `Proxy`s, and the
                # return value of our decomposition rule is no exception.
                # We need to extract the underlying `Node` from the `Proxy`
                # to use it in subsequent iterations of this transform.
                new_node = output_proxy.node
                env[node.name] = new_node
                node_processed = True

        if not node_processed:
            # Default case: just copy the node over into the new graph.
            new_node = new_graph.node_copy(node, lambda x: env[x.name])
            env[node.name] = new_node

    new_graph.lint()
    return new_graph