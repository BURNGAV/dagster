from collections import defaultdict
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from toposort import CircularDependencyError, toposort

import dagster._check as check
from dagster.core.definitions.asset_layer import AssetLayer
from dagster.core.definitions.config import ConfigMapping
from dagster.core.definitions.dependency import (
    DependencyDefinition,
    IDependencyDefinition,
    NodeHandle,
    NodeInputHandle,
    NodeInvocation,
)
from dagster.core.definitions.events import AssetKey
from dagster.core.definitions.executor_definition import ExecutorDefinition
from dagster.core.definitions.graph_definition import GraphDefinition, default_job_io_manager
from dagster.core.definitions.job_definition import JobDefinition
from dagster.core.definitions.partition import PartitionedConfig, PartitionsDefinition
from dagster.core.definitions.resource_definition import ResourceDefinition
from dagster.core.definitions.resource_requirement import ensure_requirements_satisfied
from dagster.core.errors import DagsterInvalidDefinitionError
from dagster.core.selector.subset_selector import AssetSelectionData
from dagster.utils import merge_dicts
from dagster.utils.backcompat import experimental

from .assets import AssetsDefinition
from .source_asset import SourceAsset


@experimental
def build_assets_job(
    name: str,
    assets: Iterable[AssetsDefinition],
    source_assets: Optional[Sequence[Union[SourceAsset, AssetsDefinition]]] = None,
    resource_defs: Optional[Mapping[str, ResourceDefinition]] = None,
    description: Optional[str] = None,
    config: Optional[Union[ConfigMapping, Dict[str, Any], PartitionedConfig]] = None,
    tags: Optional[Dict[str, Any]] = None,
    executor_def: Optional[ExecutorDefinition] = None,
    partitions_def: Optional[PartitionsDefinition] = None,
    _asset_selection_data: Optional[AssetSelectionData] = None,
) -> JobDefinition:
    """Builds a job that materializes the given assets.

    The dependencies between the ops in the job are determined by the asset dependencies defined
    in the metadata on the provided asset nodes.

    Args:
        name (str): The name of the job.
        assets (List[AssetsDefinition]): A list of assets or
            multi-assets - usually constructed using the :py:func:`@asset` or :py:func:`@multi_asset`
            decorator.
        source_assets (Optional[Sequence[Union[SourceAsset, AssetsDefinition]]]): A list of
            assets that are not materialized by this job, but that assets in this job depend on.
        resource_defs (Optional[Dict[str, ResourceDefinition]]): Resource defs to be included in
            this job.
        description (Optional[str]): A description of the job.

    Examples:

        .. code-block:: python

            @asset
            def asset1():
                return 5

            @asset
            def asset2(asset1):
                return my_upstream_asset + 1

            my_assets_job = build_assets_job("my_assets_job", assets=[asset1, asset2])

    Returns:
        JobDefinition: A job that materializes the given assets.
    """

    check.str_param(name, "name")
    check.iterable_param(assets, "assets", of_type=AssetsDefinition)
    source_assets = check.opt_sequence_param(
        source_assets, "source_assets", of_type=(SourceAsset, AssetsDefinition)
    )
    check.opt_str_param(description, "description")
    check.opt_inst_param(_asset_selection_data, "_asset_selection_data", AssetSelectionData)

    # figure out what partitions (if any) exist for this job
    partitions_def = partitions_def or build_job_partitions_from_assets(assets)

    resource_defs = check.opt_mapping_param(resource_defs, "resource_defs")
    resource_defs = merge_dicts({"io_manager": default_job_io_manager}, resource_defs)

    conformed_source_assets = [
        source_asset
        for asset in source_assets
        for source_asset in (
            [asset] if isinstance(asset, SourceAsset) else asset.to_source_assets()
        )
    ]
    deps, assets_defs_by_node_handle, asset_keys_by_input_handle = build_node_deps(
        assets, conformed_source_assets
    )

    # attempt to resolve cycles using multi-asset subsetting
    if _has_cycles(deps):
        assets = _attempt_resolve_cycles(assets)
        deps, assets_defs_by_node_handle, asset_keys_by_input_handle = build_node_deps(
            assets, conformed_source_assets
        )

    graph = GraphDefinition(
        name=name,
        node_defs=[asset.node_def for asset in assets],
        dependencies=deps,
        description=description,
        input_mappings=None,
        output_mappings=None,
        config=None,
    )

    # turn any AssetsDefinitions into SourceAssets
    resolved_source_assets: List[SourceAsset] = []
    for asset in source_assets or []:
        if isinstance(asset, AssetsDefinition):
            resolved_source_assets += asset.to_source_assets()
        elif isinstance(asset, SourceAsset):
            resolved_source_assets.append(asset)

    asset_layer = AssetLayer.from_graph_and_assets_node_mapping(
        graph, assets_defs_by_node_handle, resolved_source_assets, asset_keys_by_input_handle
    )

    all_resource_defs = get_all_resource_defs(assets, resolved_source_assets, resource_defs)

    return graph.to_job(
        resource_defs=all_resource_defs,
        config=config,
        tags=tags,
        executor_def=executor_def,
        partitions_def=partitions_def,
        asset_layer=asset_layer,
        _asset_selection_data=_asset_selection_data,
    )


def build_job_partitions_from_assets(
    assets: Iterable[AssetsDefinition],
) -> Optional[PartitionsDefinition]:
    assets_with_partitions_defs = [assets_def for assets_def in assets if assets_def.partitions_def]

    if len(assets_with_partitions_defs) == 0:
        return None

    first_assets_with_partitions_def: AssetsDefinition = assets_with_partitions_defs[0]
    for assets_def in assets_with_partitions_defs:
        if assets_def.partitions_def != first_assets_with_partitions_def.partitions_def:
            first_asset_key = next(iter(assets_def.keys)).to_string()
            second_asset_key = next(iter(first_assets_with_partitions_def.keys)).to_string()
            raise DagsterInvalidDefinitionError(
                "When an assets job contains multiple partitions assets, they must have the "
                f"same partitions definitions, but asset '{first_asset_key}' and asset "
                f"'{second_asset_key}' have different partitions definitions. "
            )

    return first_assets_with_partitions_def.partitions_def


def resolve_assets_def_deps(
    assets_defs: Iterable[AssetsDefinition], source_assets: Iterable[SourceAsset]
) -> Sequence[Tuple[AssetsDefinition, Mapping[str, AssetKey]]]:
    """
    For each AssetsDefinition, resolves its inputs to upstream asset keys. Matches based on either
    of two criteria:
    - The input asset key exactly matches an asset key.
    - The input asset key has one component, that component matches the final component of an asset
        key, and they're both in the same asset group.
    """
    asset_keys_by_group_and_name: Dict[Tuple[str, str], AssetKey] = {}
    for assets_def in assets_defs:
        for key in assets_def.keys:
            asset_keys_by_group_and_name[(assets_def.group_names_by_key[key], key.path[-1])] = key
    for source_asset in source_assets:
        asset_keys_by_group_and_name[
            (source_asset.group_name, source_asset.key.path[-1])
        ] = source_asset.key

    asset_keys = set(asset_keys_by_group_and_name.values())

    result: List[Tuple[AssetsDefinition, AbstractSet[AssetKey]]] = []
    for assets_def in assets_defs:
        group = (
            next(iter(assets_def.group_names_by_key.values()))
            if len(assets_def.group_names_by_key) == 1
            else None
        )

        dep_keys_by_input_name: Dict[str, AssetKey] = {}
        for input_name, upstream_asset_key in assets_def.keys_by_input_name.items():
            group_and_upstream_name = (group, upstream_asset_key.path[-1])
            if upstream_asset_key in asset_keys:
                dep_keys_by_input_name[input_name] = upstream_asset_key
            elif group is not None and group_and_upstream_name in asset_keys_by_group_and_name:
                dep_keys_by_input_name[input_name] = asset_keys_by_group_and_name[
                    group_and_upstream_name
                ]
            else:
                raise DagsterInvalidDefinitionError(
                    f"Input asset '{upstream_asset_key.to_string()}' for asset "
                    f"'{next(iter(assets_def.keys)).to_string()}' is not "
                    "produced by any of the provided asset ops and is not one of the provided "
                    "sources"
                )

        result.append((assets_def, dep_keys_by_input_name))

    return result


def build_node_deps(
    assets_defs: Iterable[AssetsDefinition], source_assets: Iterable[SourceAsset]
) -> Tuple[
    Dict[Union[str, NodeInvocation], Dict[str, IDependencyDefinition]],
    Mapping[NodeHandle, AssetsDefinition],
    Mapping[NodeInputHandle, AssetKey],
]:
    # sort so that nodes get a consistent name
    assets_defs = sorted(assets_defs, key=lambda ad: (sorted((ak for ak in ad.keys))))

    # if the same graph/op is used in multiple assets_definitions, their invocations must have
    # different names. we keep track of definitions that share a name and add a suffix to their
    # invocations to solve this issue
    collisions: Dict[str, int] = {}
    assets_defs_by_node_handle: Dict[NodeHandle, AssetsDefinition] = {}
    node_alias_and_output_by_asset_key: Dict[AssetKey, Tuple[str, str]] = {}
    for assets_def in assets_defs:
        node_name = assets_def.node_def.name
        if collisions.get(node_name):
            collisions[node_name] += 1
            node_alias = f"{node_name}_{collisions[node_name]}"
        else:
            collisions[node_name] = 1
            node_alias = node_name

        # unique handle for each AssetsDefinition
        assets_defs_by_node_handle[NodeHandle(node_alias, parent=None)] = assets_def
        for output_name, key in assets_def.keys_by_output_name.items():
            node_alias_and_output_by_asset_key[key] = (node_alias, output_name)

    dep_keys_by_input_name_by_assets_def_id = {
        id(assets_def): dep_keys
        for assets_def, dep_keys in resolve_assets_def_deps(assets_defs, source_assets)
    }

    deps: Dict[Union[str, NodeInvocation], Dict[str, IDependencyDefinition]] = {}
    asset_keys_by_input_handle: Dict[NodeInputHandle, AssetKey] = {}
    for node_handle, assets_def in assets_defs_by_node_handle.items():
        # the key that we'll use to reference the node inside this AssetsDefinition
        node_def_name = assets_def.node_def.name
        if node_handle.name != node_def_name:
            node_key = NodeInvocation(node_def_name, alias=node_handle.name)
        else:
            node_key = node_def_name
        deps[node_key] = {}

        # connect each input of this AssetsDefinition to the proper upstream node
        for input_name, upstream_asset_key in dep_keys_by_input_name_by_assets_def_id[
            id(assets_def)
        ].items():
            if upstream_asset_key in node_alias_and_output_by_asset_key:
                upstream_node_alias, upstream_output_name = node_alias_and_output_by_asset_key[
                    upstream_asset_key
                ]
                deps[node_key][input_name] = DependencyDefinition(
                    upstream_node_alias, upstream_output_name
                )

            asset_keys_by_input_handle[
                NodeInputHandle(node_handle, input_name)
            ] = upstream_asset_key

    return deps, assets_defs_by_node_handle, asset_keys_by_input_handle


def _has_cycles(deps: Dict[Union[str, NodeInvocation], Dict[str, IDependencyDefinition]]) -> bool:
    """Detect if there are cycles in a dependency dictionary."""
    try:
        node_deps: Dict[Union[str, NodeInvocation], Set[str]] = {}
        for upstream_node, downstream_deps in deps.items():
            node_deps[upstream_node] = set()
            for dep in downstream_deps.values():
                if isinstance(dep, DependencyDefinition):
                    node_deps[upstream_node].add(dep.node)
                else:
                    check.failed(f"Unexpected dependency type {type(dep)}.")
        # make sure that there is a valid topological sorting of these node dependencies
        list(toposort(node_deps))
        return False
    # only try to resolve cycles if we have a cycle
    except CircularDependencyError:
        return True


def _attempt_resolve_cycles(
    assets_defs: Iterable["AssetsDefinition"],
) -> Sequence["AssetsDefinition"]:
    """
    DFS starting at root nodes to color the asset dependency graph. Each time you leave your
    current AssetsDefinition, the color increments.

    At the end of this process, we'll have a coloring for the asset graph such that any asset which
    is downstream of another asset via a different AssetsDefinition will be guaranteed to have
    a different (greater) color.

    Once we have our coloring, if any AssetsDefinition contains assets with different colors,
    we split that AssetsDefinition into a subset for each individual color.

    This ensures that no asset that shares a node with another asset will be downstream of
    that asset via a different node (i.e. there will be no cycles).
    """
    from dagster.core.selector.subset_selector import generate_asset_dep_graph

    # get asset dependencies
    asset_deps = generate_asset_dep_graph(assets_defs)

    # index AssetsDefinitions by their asset names
    assets_defs_by_asset_name = {}
    for assets_def in assets_defs:
        for asset_key in assets_def.keys:
            assets_defs_by_asset_name[asset_key.to_user_string()] = assets_def

    # color for each asset
    colors = {}

    # recursively color an asset and all of its downstream assets
    def _dfs(name, cur_color):
        colors[name] = cur_color
        if name in assets_defs_by_asset_name:
            cur_node_asset_keys = assets_defs_by_asset_name[name].keys
        else:
            # in a SourceAsset, treat all downstream as if they're in the same node
            cur_node_asset_keys = asset_deps["downstream"][name]

        for downstream_name in asset_deps["downstream"][name]:
            # if the downstream asset is in the current node,keep the same color
            if AssetKey.from_user_string(downstream_name) in cur_node_asset_keys:
                new_color = cur_color
            else:
                new_color = cur_color + 1

            # if current color of the downstream asset is less than the new color, re-do dfs
            if colors.get(downstream_name, -1) < new_color:
                _dfs(downstream_name, new_color)

    # validate that there are no cycles in the overall asset graph
    toposorted = list(toposort(asset_deps["upstream"]))

    # dfs for each root node
    for root_name in toposorted[0]:
        _dfs(root_name, 0)

    color_mapping_by_assets_defs: Dict[AssetsDefinition, Any] = defaultdict(
        lambda: defaultdict(set)
    )
    for name, color in colors.items():
        asset_key = AssetKey.from_user_string(name)
        # ignore source assets
        if name not in assets_defs_by_asset_name:
            continue
        color_mapping_by_assets_defs[assets_defs_by_asset_name[name]][color].add(
            AssetKey.from_user_string(name)
        )

    ret = []
    for assets_def, color_mapping in color_mapping_by_assets_defs.items():
        if len(color_mapping) == 1 or not assets_def.can_subset:
            ret.append(assets_def)
        else:
            for asset_keys in color_mapping.values():
                ret.append(assets_def.subset_for(asset_keys))

    return ret


def _ensure_resources_dont_conflict(
    assets: Iterable[AssetsDefinition],
    source_assets: Sequence[SourceAsset],
    resource_defs: Mapping[str, ResourceDefinition],
) -> None:
    """Ensures that resources between assets, source assets, and provided resource dictionary do not conflict."""
    resource_defs_from_assets = {}
    all_assets: Sequence[Union[AssetsDefinition, SourceAsset]] = [*assets, *source_assets]
    for asset in all_assets:
        for resource_key, resource_def in asset.resource_defs.items():
            if resource_key not in resource_defs_from_assets:
                resource_defs_from_assets[resource_key] = resource_def
            if resource_defs_from_assets[resource_key] != resource_def:
                raise DagsterInvalidDefinitionError(
                    f"Conflicting versions of resource with key '{resource_key}' "
                    "were provided to different assets. When constructing a "
                    "job, all resource definitions provided to assets must "
                    "match by reference equality for a given key."
                )
    for resource_key, resource_def in resource_defs.items():
        if (
            resource_key != "io_manager"
            and resource_key in resource_defs_from_assets
            and resource_defs_from_assets[resource_key] != resource_def
        ):
            raise DagsterInvalidDefinitionError(
                f"resource with key '{resource_key}' provided to job "
                "conflicts with resource provided to assets. When constructing a "
                "job, all resource definitions provided must "
                "match by reference equality for a given key."
            )


def check_resources_satisfy_requirements(
    assets: Iterable[AssetsDefinition],
    source_assets: Sequence[SourceAsset],
    resource_defs: Mapping[str, ResourceDefinition],
) -> None:
    """Ensures that between the provided resources on an asset and the resource_defs mapping, that all resource requirements are satisfied.

    Note that resources provided on assets cannot satisfy resource requirements provided on other assets.
    """

    _ensure_resources_dont_conflict(assets, source_assets, resource_defs)

    all_assets: Sequence[Union[AssetsDefinition, SourceAsset]] = [*assets, *source_assets]
    for asset in all_assets:
        ensure_requirements_satisfied(
            merge_dicts(resource_defs, asset.resource_defs), list(asset.get_resource_requirements())
        )


def get_all_resource_defs(
    assets: Iterable[AssetsDefinition],
    source_assets: Sequence[SourceAsset],
    resource_defs: Mapping[str, ResourceDefinition],
) -> Dict[str, ResourceDefinition]:

    # Ensures that no resource keys conflict, and each asset has its resource requirements satisfied.
    check_resources_satisfy_requirements(assets, source_assets, resource_defs)

    all_resource_defs = dict(resource_defs)
    all_assets: Sequence[Union[AssetsDefinition, SourceAsset]] = [*assets, *source_assets]
    for asset in all_assets:
        all_resource_defs = merge_dicts(all_resource_defs, asset.resource_defs)
    return all_resource_defs
