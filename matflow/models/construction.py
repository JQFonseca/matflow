"""matflow.models.construction.py

This is the business end of matflow.

Functions for constructing model (Workflow/Task) attributes. This functionality is mostly
contained here, rather than within the Workflow/Task classes, because Workflow/Task
objects can either be constructed from scratch, or reconstituted from the persistent
workflow file. This way initialisation of these objects is always consistent, and testing
is easier. The classes themselves mainly serve to provide useful properties.

"""

import copy
from warnings import warn

import numpy as np
from hpcflow.scheduler import SunGridEngine

from matflow.config import Config
from matflow.errors import (
    IncompatibleWorkflow,
    MissingSoftware,
    WorkflowPersistenceError,
    SequenceError,
    IncompatibleSequence,
    TaskError,
    MissingMergePriority,
    IncompatibleTaskNesting,
    UnsatisfiedGroupParameter,
    MissingSchemaError,
    UnsatisfiedSchemaError,
    MissingSoftwareSourcesError,
    TaskParameterError,
)
from matflow.utils import (tile, repeat, arange, extend_index_list, flatten_list,
                           to_sub_list, get_specifier_dict)
from matflow.models.task import Task, TaskSchema
from matflow.models.software import SoftwareInstance


def normalise_local_inputs(base=None, sequences=None, is_from_file=False):
    """Validate and normalise sequences and task inputs for a given task.

    Parameters
    ----------
    base : dict, optional
    sequences : list, optional
    is_from_file : bool
        Has this task dict been loaded from a workflow file or is it associated
        with a brand new workflow?

    Returns
    -------
    inputs_lst : list of dict
        Each list item is a dict corresponding to a particular input parameter. Dict keys
        are:
            name : str
                The name of the input parameter.
            nest_idx : int
                The intra-task nesting index. For parameters that are specified in the
                base dict (non-sequence parameters), this is set to -1. For parameters
                specified in the sequences list, this is whatever is specified by the user
                or 0 if not specified.
            vals : list
                List of values for this input parameter. This will be a list of length one
                for input parameters specified within the base dict.

    """

    if base is None:
        base = {}
    if sequences is None:
        sequences = []

    if not isinstance(sequences, list):
        raise SequenceError('`sequences` must be a list.')

    nest_req = True if len(sequences) > 1 else False

    req_seq_keys = ['name', 'vals']
    allowed_seq_keys = req_seq_keys + ['nest_idx']

    prev_num_vals = None
    prev_nest = None
    inputs_lst = []
    for seq in sequences:

        miss_keys = list(set(req_seq_keys) - set(seq.keys()))
        if miss_keys:
            miss_keys_fmt = ', '.join([f'"{i}"' for i in miss_keys])
            msg = f'Missing keys from sequence definition: {miss_keys_fmt}.'
            raise SequenceError(msg)

        bad_keys = list(set(seq.keys()) - set(allowed_seq_keys))
        if bad_keys:
            bad_keys_fmt = ', '.join([f'"{i}"' for i in bad_keys])
            raise SequenceError(f'Unknown keys from sequence definition: {bad_keys_fmt}.')

        if not isinstance(seq['vals'], list):
            raise SequenceError(f'Sequence "{seq["name"]}" `vals` must be a list.')

        if 'nest_idx' in seq:
            if not isinstance(seq['nest_idx'], int) or (seq['nest_idx'] < 0):
                msg = (f'`nest_idx` must be a positive integer or zero for sequence '
                       f'"{seq["name"]}"')
                raise SequenceError(msg)

        if nest_req:
            if 'nest_idx' not in seq:
                msg = f'`nest_idx` is required for sequence "{seq["name"]}".'
                raise SequenceError(msg)
        else:
            if 'nest_idx' in seq and not is_from_file:
                msg = (f'`nest_idx` (specified as {seq["nest_idx"]}) is not required for '
                       f'sequence "{seq["name"]}"; resetting to zero.')
                warn(msg)
            seq['nest_idx'] = 0  # set a default

        nest = seq['nest_idx']

        num_vals = len(seq['vals'])
        if prev_num_vals and nest == prev_nest:
            # For same nest_idx, sequences must have the same lengths:
            if num_vals != prev_num_vals:
                msg = (f'Sequence "{seq["name"]}" shares a `nest_idx` with another '
                       f'sequence but has a different number of values.')
                raise IncompatibleSequence(msg)

        prev_num_vals = num_vals
        prev_nest = nest
        inputs_lst.append(copy.deepcopy(seq))

    for in_name, in_val in base.items():
        inputs_lst.append({
            'name': in_name,
            'nest_idx': -1,
            'vals': [copy.deepcopy(in_val)],
        })

    inputs_lst.sort(key=lambda x: x['nest_idx'])

    return inputs_lst


def get_local_inputs(task, is_from_file):
    """Combine task base/sequences/repeats to get the locally defined inputs for a task.

    Parameters
    ----------
    task : dict
        Dict representing a task.
    is_from_file : bool
        Has this task dict been loaded from a workflow file or is it associated
        with a brand new workflow?

    """

    base = task['base']
    num_repeats = task['repeats'] or 1
    sequences = task['sequences']
    nest = task['nest']
    merge_priority = task['merge_priority']
    groups = task['groups']
    schema = task['schema']

    inputs_lst = normalise_local_inputs(base, sequences, is_from_file)
    default_inputs_lst = []

    for input_dict in schema.inputs:
        if 'default' in input_dict:
            default_inputs_lst.append({
                'name': input_dict['name'],
                'nest_idx': -1,
                'vals': [input_dict['default']],
            })

    defined_inputs = [i['name'] for i in inputs_lst]
    schema.check_surplus_inputs(defined_inputs)

    # Find the number of elements associated with this task due to its local input
    # sequences:
    inputs_lst.sort(key=lambda x: x['nest_idx'])
    if inputs_lst:
        lengths = [len(i['vals']) for i in inputs_lst]
        total_len = len(inputs_lst[0]['vals'])
        for idx, i in enumerate(inputs_lst[1:], 1):
            if i['nest_idx'] > inputs_lst[idx - 1]['nest_idx']:
                total_len *= len(i['vals'])

        prev_reps = total_len
        prev_tile = 1
        prev_nest = None

    else:
        total_len = 1

    local_ins = {'inputs': {}, 'default_inputs': {}}
    repeats_idx = [0]

    for idx, input_i in enumerate(inputs_lst):

        if (prev_nest is None) or (input_i['nest_idx'] > prev_nest):
            rep_i = int(prev_reps / lengths[idx])
            tile_i = int(total_len / (lengths[idx] * rep_i))
            prev_nest = input_i['nest_idx']
        else:
            rep_i = prev_reps
            tile_i = prev_tile

        vals_idx = tile(repeat(arange(lengths[idx]), rep_i), tile_i)
        vals_idx = repeat(vals_idx, num_repeats)
        repeats_idx = tile(arange(num_repeats), total_len)

        local_ins['inputs'].update({
            input_i['name']: {
                'vals': input_i['vals'],
                'vals_idx': vals_idx,
            }
        })
        prev_reps = rep_i
        prev_tile = tile_i

    local_ins['repeats_idx'] = repeats_idx

    for def_input_i in default_inputs_lst:
        local_ins['default_inputs'].update({
            def_input_i['name']: {
                'vals': def_input_i['vals'],
                'vals_idx': repeat([0] * total_len, num_repeats),
            }
        })

    allowed_grp = schema.input_names + ['repeats']
    allowed_grp_fmt = ', '.join([f'"{i}"' for i in allowed_grp])

    def_group = {'default': {'nest': nest, 'group_by': allowed_grp}}
    if merge_priority is not None:
        def_group['default'].update({'merge_priority': merge_priority})

    user_groups = {}
    for group_name, group in (groups or {}).items():

        if 'group_by' not in group:
            raise ValueError(f'Missing `group_by` key in group {group_name}.')

        for param in group['group_by']:
            if param not in allowed_grp:
                msg = (f'Parameter "{param}" cannot be grouped, because it '
                       f'has no specified values. Allowed group values are: '
                       f'{allowed_grp_fmt}.')
                raise ValueError(msg)

        user_groups.update({f'user_group_{group_name}': group})

    all_groups = {**def_group, **user_groups}

    local_ins['groups'] = all_groups
    local_ins['length'] = total_len * num_repeats

    return local_ins


def get_software_instance(software, run_options, all_software, type_label=''):
    """Find a software instance in the software.yml file that matches the software
    requirements of a given task.

    Parameters
    ----------
    software : dict
        Dict with the following keys:
            name : str
                Name of the software whose SoftwareInstance is to be returned.
            label : str
                Additional specifier used to distinguish, e.g., a software version.
            options : list of str
                Additional specifiers used to state additional requirements.
    run_options : dict
        Dict with keys:
            num_cores : int
                Number of cores specified in the task.
            **scheduler_options
                Any other options to be passed to the scheduler.
    all_software : dict of list of SoftwareInstance
        Dict whose keys are software names and whose values are lists of SoftwareInstance
        objects.

    Returns
    -------
    SoftwareInstance
        The first matching software instance.

    Raises
    ------
    MissingSoftware
        If no matching software instance can be found.

    """
    match = None
    for name, instances in all_software.items():

        if name != SoftwareInstance.get_software_safe(software['name']):
            continue

        for inst in instances:

            if run_options['num_cores'] not in inst.cores_range:
                continue
            if inst.label != software['label']:
                continue
            if (set(software['options']) - set(inst.options)):
                continue

            # Check no conflicting scheduler options
            keep_looking = False
            for k, v in inst.required_scheduler_options.items():
                if k in run_options and v != run_options[k]:
                    keep_looking = True
                    break

            if keep_looking:
                continue
            else:
                match = inst
                break

        if match:
            break

    if match:
        if run_options['num_cores'] > 1:
            all_run_opts = {**match.required_scheduler_options, **run_options}
            # (SGE specific):
            if 'pe' not in all_run_opts:
                msg = ('Parallel environment (`pe`) key must be specified in '
                       f'`run_options{type_label}`, since `num_cores > 1`.')
                raise TaskError(msg)
    else:
        msg = (f'Could not find suitable software "{software["name"]}", with '
               f'`num_cores={run_options["num_cores"]}` and `label={software["label"]}`.')
        raise MissingSoftware(msg)

    return match


def get_input_dependency(task_info_lst, input_dict, input_task_idx):

    param_name = input_dict['name']
    param_context = input_dict['context']
    param_task = task_info_lst[input_task_idx]

    downstream_task_context = param_task['context']
    downstream_task_name = param_task['name']

    # Initially collate as a list of dependencies. Then apply some rules to find the
    # preferred single dependency.
    input_dependency = []

    for task_idx, task_info in enumerate(task_info_lst):

        if task_idx == input_task_idx:
            continue

        upstream_context = task_info['context']

        # Determine if a dependency is allowed to exist between the given parameter and
        # this task, by considering the parameter context, downstream task context and
        # upstream task context:
        if (
            param_context == upstream_context or (
                (upstream_context == downstream_task_context) and (param_context is None)
            ) or (
                upstream_context == ''
            )
        ):
            # A dependency may exist! The parameter as an output in an upstream task takes
            #  precedence over the parameter as an input in that same task:
            if param_name in task_info['schema'].outputs:
                input_dependency.append({
                    'from_task': task_idx,
                    'dependency_type': 'output',
                    'is_parameter_modifying_task': False,
                })
            elif (
                param_name in task_info['schema'].input_names and
                param_name in task_info['local_inputs']['inputs']
            ):
                input_dependency.append({
                    'from_task': task_idx,
                    'dependency_type': 'input',
                    'is_parameter_modifying_task': False,
                })

            # Make a note if this task is a parameter-modifying task for this parameter,
            # i.e. the parameter is both an input and an output:
            if (
                param_name in task_info['schema'].outputs and
                param_name in task_info['schema'].input_names
            ):
                input_dependency[-1].update({'is_parameter_modifying_task': True})

    # Make sure only a single dependency exists.

    # An "input" dependency_type (meaning this parameter derives from an input in an
    # upstream task) must be locally defined in only one task, otherwise the dependency
    # is ill-defined:
    dep_type_input_count = sum([i['dependency_type'] == 'input'
                                for i in input_dependency])
    if dep_type_input_count > 1:
        msg = (
            f'Task input parameter "{param_name}" from task "{downstream_task_name}" with '
            f'task context "{downstream_task_context}" is found as an input parameter '
            f'for multiple tasks, which makes the task dependency ill-defined.'
        )
        raise IncompatibleWorkflow(msg)

    # An "output" dependency_type (meaning this parameter derives from an output in an
    # upstream task) must be also be singular, since otherwise the dependency is again
    # ill-defined:
    dep_type_output_count = sum([i['dependency_type'] == 'output'
                                 for i in input_dependency])
    if dep_type_output_count > 1:
        msg = (
            f'Task input parameter "{param_name}" from task "{downstream_task_name}" with '
            f'task context "{downstream_task_context}" is found as an output parameter '
            f'for multiple tasks, which makes the task dependency ill-defined.'
        )
        raise IncompatibleWorkflow(msg)

    return input_dependency[0] if input_dependency else None


def get_dependency_idx(task_info_lst):
    """Find the dependencies between tasks.

    Parameters
    ----------
    task_info_lst : list of dict
        Each dict must have keys:
            context : str
            schema : TaskSchema
            local_inputs : dict

    Returns
    -------
    dependency_idx : list of list of int
        Each element, which corresponds to a given task in `task_info_list`,
        lists the task indices upon which the given task depends.

    Notes
    -----
    - Two conditions must be met for a task (the downstream task) to be recorded
      as depending on another (upstream) task:
          1) one of the downstream task's input parameters must be one of the
             upstream task's output parameters;
          2) EITHER:
              - One of the downstream task's input parameters shares a context
                with the upstream task, OR
              - The upstream and downstream task share the same context, and,
                for any downstream task input parameter, the parameter context
                is `None`.
              - The upstream task context is default (i.e. '').

    """

    dependency_idx = []
    for task_idx, task_info in enumerate(task_info_lst):

        task_name, task_context = task_info['name'], task_info['context']

        dep_idx_i = {
            'task_dependencies': [],
            'parameter_dependencies': {},
        }

        # Find which tasks this task depends on:
        task_dep_idx = []
        for input_dict in task_info['schema'].inputs:

            input_name = input_dict['name']
            is_locally_defined = input_name in task_info['local_inputs']['inputs']
            default_defined = input_name in task_info['local_inputs']['default_inputs']
            input_dependency = get_input_dependency(task_info_lst, input_dict, task_idx)

            add_input_dep = False
            if is_locally_defined:

                if input_dependency:
                    in_dep_task = task_info_lst[input_dependency['from_task']]
                    in_dep_task_name = in_dep_task['name']
                    in_dep_task_context = in_dep_task['context']
                    msg = (
                        f'Input parameter "{input_name}" for task "{task_name}" with '
                        f'task context "{task_context}" has both a local value and a '
                        f'non-local value. The non-local value is derived from an '
                        f'{input_dependency["dependency_type"]} of task '
                        f'"{in_dep_task_name}" with task context '
                        f'"{in_dep_task_context}". The local value will be used.'
                    )
                    warn(msg)

            elif input_dependency:
                add_input_dep = True

            elif default_defined:
                # Move the default value into the local inputs:
                task_info_lst[task_idx]['local_inputs']['inputs'][input_name] = (
                    copy.deepcopy(task_info['local_inputs']['default_inputs'][input_name])
                )

            else:
                msg = (f'Task input "{input_name}" for task "{task_name}" with task '
                       f'context "{task_context}" is missing. A value must be specified '
                       f'locally, or a default value must be provided in the schema, or '
                       f'an additional task should be added to the workflow that '
                       f'generates the parameter')
                raise TaskParameterError(msg)

            if add_input_dep:
                dep_idx_i['parameter_dependencies'].update({input_name: input_dependency})
                task_dep_idx.append(input_dependency['from_task'])

        dep_idx_i['task_dependencies'] = list(set(task_dep_idx))
        dependency_idx.append(dep_idx_i)

    # Check for circular dependencies in task inputs/outputs:
    all_deps = []
    for idx, deps in enumerate(dependency_idx):
        for i in deps['task_dependencies']:
            all_deps.append(tuple(sorted([idx, i])))

    if len(all_deps) != len(set(all_deps)):
        msg = (f'Workflow tasks are circularly dependent! `dependency_idx` is: '
               f'{dependency_idx}')
        raise IncompatibleWorkflow(msg)

    return dependency_idx


def validate_run_options(run_opts, type_label=''):

    # SGE specific:
    ALLOWED = SunGridEngine.ALLOWED_USER_OPTS + ['num_cores', 'alternate_scratch']
    if 'preparation' in type_label or 'processing' in type_label:
        ALLOWED += ['job_array']

    bad_keys = set(run_opts.keys()) - set(ALLOWED)
    if bad_keys:
        bad_keys_fmt = ', '.join([f'{i!r}' for i in bad_keys])
        raise TaskError(f'Run options not known: {bad_keys_fmt}.')

    run_opts = copy.deepcopy(run_opts)

    if 'preparation' in type_label:
        if not run_opts:
            run_opts = {
                **Config.get('default_sticky_preparation_run_options'),
                **Config.get('default_preparation_run_options'),
            }
        else:
            run_opts = {
                **Config.get('default_sticky_preparation_run_options'),
                **run_opts
            }
    elif 'processing' in type_label:
        if not run_opts:
            run_opts = {
                **Config.get('default_sticky_processing_run_options'),
                **Config.get('default_processing_run_options'),
            }
        else:
            run_opts = {
                **Config.get('default_sticky_processing_run_options'),
                **run_opts
            }
    else:
        if not run_opts:
            run_opts = {
                **Config.get('default_sticky_run_options'),
                **Config.get('default_run_options'),
            }
        else:
            run_opts = {
                **Config.get('default_sticky_run_options'),
                **run_opts
            }

    if 'num_cores' not in run_opts:
        run_opts.update({'num_cores': 1})

    if run_opts['num_cores'] <= 0:
        msg = (f'Specify `num_cores` (in `run_options{type_label}`) as an integer '
               f'greater than 0.')
        raise TaskError(msg)
    elif 'pe' in run_opts:
        msg = (f'No need to specify parallel environment (`pe`) in '
               f'`run_options{type_label}`, since `num_cores=1`.')
        raise TaskError(msg)

    return run_opts


def validate_task_dict(task, is_from_file, all_software, all_task_schemas,
                       all_sources_maps):
    """Validate a task dict.

    Parameters
    ----------
    task : dict
        Keys are either those specified for the task when generating a new
        workflow, or the full set of task attributes loaded from a task within
        a workflow file.
    is_from_file : bool
        Has this task dict been loaded from a workflow file or is it associated
        with a brand new workflow?
    all_software : dict of list of SoftwareInstance
        Dict whose keys are software names and whose values are lists of SoftwareInstance
        objects.
    all_task_schemas : dict of (tuple : TaskSchema)
        All available TaskSchema objects, keyed by a (name, method, software) tuple.
    all_sources_maps : dict of (tuple : dict)
        All available sources maps.

    Returns
    -------
    task_list : list of dict
        Copy of original `task` dict, with default keys added. If `is_from_file=False`,
        `local_inputs` and `schema` are added. Usually this will just be a list of length
        one. However, there are some scenarios where multiple task dicts will be returned.
        For example, if multiple `contexts` are specified, the task will be repeated,
        once for each `context`.

    """

    if not isinstance(task, dict):
        raise TaskError(f'Task definition must be a dict, but "{type(task)}" given.')

    if is_from_file:
        req_keys = [
            'id',
            'name',
            'method',
            'elements',
            'software_instance',
            'prepare_software_instance',
            'process_software_instance',
            'task_idx',
            'run_options',
            'prepare_run_options',
            'process_run_options',
            'status',
            'stats',
            'context',
            'local_inputs',
            'schema',
            'resource_usage',
            'base',
            'sequences',
            'repeats',
            'groups',
            'nest',
            'merge_priority',
            'output_map_options',
            'command_pathway_idx',
        ]
        good_keys = req_keys
        def_keys = {}
    else:
        req_keys = ['name', 'software', 'method']
        good_keys = [
            'software',
            'run_options',
            'stats',
            'context',
            'contexts',
            'base',
            'sequences',
            'repeats',
            'groups',
            'nest',
            'merge_priority',
            'output_map_options',
        ] + req_keys

        def_keys = {
            'run_options': {},
            'stats': True,
            'base': None,
            'sequences': None,
            'repeats': 1,
            'groups': None,
            'merge_priority': None,
            'nest': True,
            'output_map_options': {},
        }

    miss_keys = list(set(req_keys) - set(task.keys()))
    if miss_keys:
        miss_keys_fmt = ', '.join([f'"{i}"' for i in miss_keys])
        msg = (f'Missing keys in Task definition (`is_from_file={is_from_file}`): '
               f'{miss_keys_fmt}.')
        raise TaskError(msg)

    bad_keys = list(set(task.keys()) - set(good_keys))
    if bad_keys:
        bad_keys_fmt = ', '.join([f'"{i}"' for i in bad_keys])
        msg = (f'Unknown keys in Task definition (`is_from_file={is_from_file}`): '
               f'{bad_keys_fmt}.')
        raise TaskError(msg)

    task = {**def_keys, **copy.deepcopy(task)}

    all_run_opts = task.pop('run_options')
    prep_run_opts = all_run_opts.pop('preparation', {})
    proc_run_opts = all_run_opts.pop('processing', {})
    task['prepare_run_options'] = validate_run_options(prep_run_opts, '.preparation')
    task['process_run_options'] = validate_run_options(proc_run_opts, '.processing')
    task['run_options'] = validate_run_options(all_run_opts)

    # Make TaskSchema:
    if is_from_file:
        # Load from file (don't use task schemas existing on this installation):
        schema = TaskSchema(**task['schema'])

    else:

        # Normalise for multiple `contexts`:
        if 'context' in task and 'contexts' in task:
            msg = ('Specify exactly one of `context` and `contexts` (these keys are '
                   'equivalent).')
            raise TaskError(msg)

        elif ('context' not in task) and ('contexts' not in task):
            task['contexts'] = ''

        elif 'context' in task:
            task['contexts'] = task.pop('context')

        if not isinstance(task['contexts'], list):
            task['contexts'] = [task['contexts']]

        # Find the software instance:
        software = get_specifier_dict(
            task.pop('software'),
            name_key='name',
            base_key='label',
            list_specifiers=['options'],
            defaults={'label': None, 'options': []},
        )

        soft_inst = get_software_instance(
            software,
            task['run_options'],
            all_software,
        )
        prepare_soft_inst = get_software_instance(
            software,
            task['prepare_run_options'],
            all_software,
            type_label='.prepare',
        )
        process_soft_inst = get_software_instance(
            software,
            task['process_run_options'],
            all_software,
            type_label='.process',
        )

        schema_key = (task['name'], task['method'], soft_inst.software)

        task['software_instance'] = soft_inst
        task['prepare_software_instance'] = prepare_soft_inst
        task['process_software_instance'] = process_soft_inst

        # Find the schema:
        schema = all_task_schemas.get(schema_key)
        if not schema:
            msg = (f'No matching task schema found for task name "{task["name"]}" with '
                   f'method "{task["method"]}" and software "{soft_inst.software}".')
            raise MissingSchemaError(msg)
        if not Config.get('schema_validity')[schema_key][0]:
            msg = (f'Task schema invalid for task schema name "{task["name"]}" with '
                   f'method "{task["method"]}" and software "{soft_inst.software}": '
                   f'{Config.get("schema_validity")[schema_key][1]}')
            raise UnsatisfiedSchemaError(msg)

        # Check any sources required by the main software instance are defined in the
        # sources map:
        soft_inst.validate_source_maps(*schema_key, all_sources_maps)

    task['schema'] = schema

    if is_from_file:
        task_list = [task]

    else:
        task_list = []
        for context in task['contexts']:
            task_copy = copy.deepcopy(task)
            task_copy['context'] = context
            del task_copy['contexts']
            task_list.append(task_copy)

    return task_list


def order_tasks(task_lst):
    """Order tasks according to their dependencies.

    Parameters
    ----------
    task_lst : list of dict
        List of dicts representing the validated task attributes. Each dict
        must contain the keys:
            context : str
            schema : TaskSchema
            output_map_options

    Returns
    -------
    task_lst_srt : list of dict
        Task list ordered to satisfy the dependencies between tasks, such that
        a task that depends on another task is placed after that other task.
    dep_idx_srt : list of list of int
        List of length equal to original `task_lst`, whose elements are integer
        lists that link a given task to the indices of tasks upon which it
        depends.

    """

    for i_idx, i in enumerate(task_lst):
        out_opts = i['schema'].validate_output_map_options(i['output_map_options'])
        task_lst[i_idx]['output_map_options'] = out_opts

    dep_idx = get_dependency_idx(task_lst)

    # Find the index at which each task must be positioned to satisfy input
    # dependencies, and reorder tasks (and `dep_idx`!):
    min_idx = [max(i['task_dependencies'] or [0]) + 1 for i in dep_idx]
    task_srt_idx = np.argsort(min_idx)

    # Reorder tasks:
    task_lst_srt = [task_lst[i] for i in task_srt_idx]

    # Reorder dep_idx list:
    dep_idx_srt = [dep_idx[i] for i in task_srt_idx]

    # Dict whose keys are old task index and values are new task index:
    task_srt_idx_map = {old_idx: new_idx for old_idx, new_idx in enumerate(task_srt_idx)}

    # Remap `task_dependencies` and `from_task` in each item of dep_idx:
    for idx, dep_idx_i in enumerate(dep_idx_srt):
        dep_idx_i['task_dependencies'] = [
            task_srt_idx_map[i] for i in dep_idx_i['task_dependencies']
        ]
        for param_name, param_dep_val in dep_idx_i['parameter_dependencies'].items():
            dep_idx_i['parameter_dependencies'][param_name]['from_task'] = (
                task_srt_idx_map[param_dep_val['from_task']]
            )

    # Add sorted task idx:
    for idx, i in enumerate(task_lst_srt):
        i['task_idx'] = idx

    return task_lst_srt, dep_idx_srt


def set_default_nesting(task_lst, dep_idx):
    """Set default `nest` and `merge_priority` for each task."""

    for idx, downstream_tsk in enumerate(task_lst):

        # Do any further downstream tasks depend on this task?
        depended_on = False
        for dep_idx_i in dep_idx[(idx + 1):]:
            if idx in dep_idx_i:
                depended_on = True
                break

        if not depended_on and downstream_tsk.get('nest') is not None:
            msg = '`nest` value is specified but not required for task "{}".'
            warn(msg.format(downstream_tsk['name']))

        # Add `nest: True` by default to this task if nesting is not specified, and if at
        # least one further downstream task depends on this task:
        if downstream_tsk.get('nest', None) is None:
            if depended_on:
                downstream_tsk['nest'] = True


def resolve_group(group, local_inputs, repeats_idx):

    # TODO (later): allow `group_by` on inputs from upstream tasks?
    # See: https://github.com/LightForm-group/matflow/issues/10

    combined_arr = []
    new_group_by = []
    for i in group['group_by']:
        if (i != 'repeats') and (i not in local_inputs):
            # Can only group on locally parametrised inputs.
            continue
        else:
            new_group_by.append(i)
        if i != 'repeats':
            combined_arr.append(local_inputs[i]['vals_idx'])
        else:
            combined_arr.append([(i or -1) for i in repeats_idx])

    if combined_arr:
        combined_arr = np.vstack(combined_arr)
        _, group_idx = np.unique(combined_arr, axis=1, return_inverse=True)
        group_elem_idx = [list(np.where(group_idx == i)[0])
                          for i in range(max(group_idx) + 1)]
        group_idx = list(group_idx)
    else:
        length = len(repeats_idx)
        group_idx = [0] * length
        group_elem_idx = [arange(length)]

    group_resolved = copy.deepcopy(group)
    group_resolved.update({
        'group_idx_per_iteration': group_idx,
        'group_element_idx_per_iteration': group_elem_idx,
        'num_groups_per_iteration': len(group_elem_idx),
        'group_size_per_iteration': len(group_elem_idx[0]),
        'group_by': new_group_by,
        'num_groups': len(group_elem_idx),
    })

    if 'merge_priority' not in group_resolved:
        group_resolved.update({'merge_priority': None})

    return group_resolved


def get_element_idx(task_lst, dep_idx, num_iterations):
    """For each task, find the element indices that determine the elements to be used
    (i.e from upstream tasks) to populate task inputs.

    Parameters
    ----------
    task_lst : list of dict
        Ordered task list. Each dict must have the following keys:
            name : str
            local_inputs : list
                List of dicts with keys:
                    length : int
                    repeats_idx : list
                    inputs : dict
                    groups : dict
            schema : TaskSchema
            task_idx : int

    dep_idx : list of dict
        List of length equal to `task_lst`, whose elements are ... TODO. ..integer lists that link a
        given task to the indices of tasks upon which it depends.

    Returns
    -------
    element_idx : list of dict

    """

    # TODO: need to validate (somewhere) any specified merge_priority is >= 0?
    # TODO: check that local_inputs has an empty `inputs` dict when no local inputs
    # defined.

    # todo ensure default nest and merge_priority are set on each group (in local_inputs).

    element_idx = []
    for idx, downstream_task in enumerate(task_lst):

        upstream_tasks = [task_lst[i] for i in dep_idx[idx]['task_dependencies']]

        # local inputs dict:
        loc_in = downstream_task['local_inputs']
        schema = downstream_task['schema']

        if not upstream_tasks:
            # This task does not depend on any other tasks.
            groups = {}
            for group_name, group in loc_in['groups'].items():
                group = resolve_group(group, loc_in['inputs'], loc_in['repeats_idx'])
                groups.update({group_name: group})

            input_idx = arange(loc_in['length'])
            elem_idx_i = {
                'num_elements': loc_in['length'],
                'num_elements_per_iteration': loc_in['length'],
                'num_iterations': num_iterations,
                'groups': groups,
                'inputs': {i: {'input_idx': input_idx} for i in loc_in['inputs']},
                'iteration_idx': [0] * loc_in['length'],
            }

        else:
            # This task depends on other tasks.
            ins_local = list(loc_in['inputs'].keys())
            ins_non_local = [i for i in schema.inputs if i['name'] not in ins_local]

            # Get the (local inputs) group dict for each `ins_non_local` (from upstream
            # tasks):
            input_groups = {}
            for non_loc_inp in ins_non_local:
                input_alias = non_loc_inp['alias']
                input_name = non_loc_inp['name']
                input_context = non_loc_inp['context']
                group_name = schema.get_input_by_alias(input_alias)['group']
                for up_task in upstream_tasks:
                    if input_context is not None:
                        if up_task['context'] != input_context:
                            continue
                    if (
                        input_name in up_task['schema'].outputs or
                        (
                            input_name in up_task['schema'].input_names and
                            input_name in up_task['local_inputs']['inputs']
                        )
                    ):
                        group_name_ = group_name
                        if group_name != 'default':
                            group_name_ = 'user_group_' + group_name
                        group_dict = element_idx[up_task['task_idx']]['groups']
                        group_names_fmt = ', '.join([f'"{i}"' for i in group_dict.keys()])
                        group_dat = group_dict.get(group_name_)
                        if group_dat is None:
                            msg = (f'No group "{group_name}" defined in the workflow for '
                                   f'input "{input_name}". Defined groups are: '
                                   f'{group_names_fmt}.')
                            raise UnsatisfiedGroupParameter(msg)
                        input_groups.update({
                            input_alias: {
                                **group_dat,
                                'group_name': group_name,
                                'task_idx': up_task['task_idx'],
                                'task_name': up_task['name'],
                            }
                        })
                        break

            is_nesting_mixed = len(set([i['nest'] for i in input_groups.values()])) > 1
            for input_alias, group_info in input_groups.items():

                if group_info['merge_priority'] is None and is_nesting_mixed:
                    raise MissingMergePriority(
                        f'`merge_priority` for group ("{group_info["group_name"]}") of '
                        f'input "{input_alias}" from task "{group_info["task_name"]}" must'
                        f' be specified because nesting is mixed. (Attempting to merge '
                        f'into task "{downstream_task["name"]}").'
                    )

                elif group_info['merge_priority'] is not None and not is_nesting_mixed:
                    warn(
                        f'`merge_priority` for group ("{group_info["group_name"]}") of '
                        f'input "{input_alias}" from task "{group_info["task_name"]}" is '
                        f'specified but not required because nesting is not mixed. '
                        f'(Merging into task "{downstream_task["name"]}").'
                    )

            all_mp = {k: (v['merge_priority'] or 0) for k, v in input_groups.items()}
            merging_order = [i[0] for i in sorted(all_mp.items(), key=lambda j: j[1])]

            # Cannot propagate groups if this task has elements that are sourced from
            # multiple upstream elements (group idx would be ill-defined). Keys are
            # task_idx:
            non_unit_group_sizes = {}

            ins_dict = {}
            groups = {}  # groups defined on the downstream task
            consumed_groups = []
            for merge_order_idx, input_alias in enumerate(merging_order):

                in_group = input_groups[input_alias]
                if in_group['group_name'] != 'default':
                    consumed_groups.append('user_group_' + in_group['group_name'])

                incoming_size = in_group['num_groups_per_iteration']
                group_size = in_group['group_size_per_iteration']
                if group_size > 1:
                    non_unit_group_sizes.update({in_group['task_idx']: True})
                elif in_group['task_idx'] not in non_unit_group_sizes:
                    non_unit_group_sizes.update({in_group['task_idx']: False})

                if merge_order_idx == 0:
                    if loc_in['inputs']:
                        existing_size = loc_in['length']
                        repeats_idx = loc_in['repeats_idx']
                        input_idx = arange(existing_size)
                        for i in loc_in['inputs']:
                            inp_alias = [j['alias'] for j in schema.inputs
                                         if j['name'] == i][0]
                            ins_dict.update({inp_alias: {'input_idx': input_idx}})

                        for group_name, group in loc_in['groups'].items():
                            group = resolve_group(group, loc_in['inputs'], repeats_idx)
                            groups.update({group_name: group})

                    else:
                        existing_size = incoming_size
                        repeats_idx = [None] * existing_size
                        ins_dict.update({
                            input_alias: {
                                'task_idx': in_group['task_idx'],
                                'group': in_group['group_name'],
                                'element_idx': in_group['group_element_idx_per_iteration'],
                            }
                        })
                        continue

                if in_group['nest']:

                    # Repeat existing:
                    for k, v in ins_dict.items():
                        if 'task_idx' in v:
                            elems_idx = repeat(v['element_idx'], incoming_size)
                            ins_dict[k]['element_idx'] = elems_idx
                        else:
                            input_idx = repeat(ins_dict[k]['input_idx'], incoming_size)
                            ins_dict[k]['input_idx'] = input_idx

                    # Tile incoming:
                    ins_dict.update({
                        input_alias: {
                            'task_idx': in_group['task_idx'],
                            'group': in_group['group_name'],
                            'element_idx': tile(
                                in_group['group_element_idx_per_iteration'],
                                existing_size
                            ),
                        }
                    })

                    # Generate new groups for each group name:
                    for g_name, g in groups.items():

                        new_g_idx = extend_index_list(
                            g['group_idx_per_iteration'], incoming_size)
                        new_ge_idx = to_sub_list(
                            extend_index_list(
                                flatten_list(g['group_element_idx_per_iteration']),
                                incoming_size),
                            g['group_size_per_iteration']
                        )
                        new_num_groups = g['num_groups_per_iteration'] * incoming_size

                        groups[g_name]['group_idx_per_iteration'] = new_g_idx
                        groups[g_name]['group_element_idx_per_iteration'] = new_ge_idx
                        groups[g_name]['num_groups_per_iteration'] = new_num_groups
                        groups[g_name]['num_groups'] = new_num_groups

                    existing_size *= incoming_size

                else:

                    if incoming_size != existing_size:
                        msg = (
                            f'Cannot merge input "{input_alias}" from task '
                            f'"{in_group["task_name"]}" and group '
                            f'"{in_group["group_name"]}" into task '
                            f'"{downstream_task["name"]}". Input has '
                            f'{incoming_size} elements, but current task has (at this '
                            f'point in the merge process) {existing_size} elements.'
                        )
                        raise IncompatibleTaskNesting(msg)

                    ins_dict.update({
                        input_alias: {
                            'task_idx': in_group['task_idx'],
                            'group': in_group['group_name'],
                            'element_idx': in_group['group_element_idx_per_iteration'],
                        }
                    })

            # Try to propagate into this task any non-default groups from dependent tasks:
            prop_groups = {}
            for up_task in upstream_tasks:
                for k, v in element_idx[up_task['task_idx']]['groups'].items():
                    if (
                        k != 'default' and
                        k not in groups and
                        k not in consumed_groups
                    ):
                        if not non_unit_group_sizes[up_task['task_idx']]:
                            prop_groups.update({k: copy.deepcopy(v)})
                        else:
                            msg = (
                                f'Cannot propagate group "{k}" from task '
                                f'"{up_task["name"]}" into task '
                                f'"{downstream_task["name"]}", because elements of task '
                                f'"{downstream_task["name"]}" are sourced from non-unit-'
                                f'sized element groups from task "{up_task["name"]}".'
                            )
                            raise IncompatibleWorkflow(msg)

            for group_name, g in prop_groups.items():

                group_reps = existing_size // (g['num_groups_per_iteration']
                                               * g['group_size_per_iteration'])
                new_g_idx = extend_index_list(g['group_idx_per_iteration'], group_reps)
                new_ge_idx = to_sub_list(
                    extend_index_list(
                        flatten_list(g['group_element_idx_per_iteration']),
                        group_reps),
                    g['group_size_per_iteration']
                )
                new_num_groups = g['num_groups_per_iteration'] * group_reps

                prop_groups[group_name]['group_idx_per_iteration'] = new_g_idx
                prop_groups[group_name]['group_element_idx_per_iteration'] = new_ge_idx
                prop_groups[group_name]['num_groups_per_iteration'] = new_num_groups
                prop_groups[group_name]['num_groups'] = new_num_groups

            all_groups = {**groups, **prop_groups}

            elem_idx_i = {
                'num_elements': existing_size,
                'num_elements_per_iteration': existing_size,
                'num_iterations': num_iterations,
                'inputs': ins_dict,
                'groups': all_groups,
                'iteration_idx': [0] * existing_size,
            }

        element_idx.append(elem_idx_i)

    # Add iterations:
    for idx, elem_idx_i in enumerate(element_idx):

        new_iter_idx = [
            iter_idx
            for iter_idx in range(elem_idx_i['num_iterations'])
            for _ in range(elem_idx_i['num_elements_per_iteration'])
        ]
        element_idx[idx]['iteration_idx'] = new_iter_idx
        element_idx[idx]['num_elements'] = len(new_iter_idx)

        for input_alias, inputs_idx in elem_idx_i['inputs'].items():

            ins_task_idx = inputs_idx.get('task_idx')

            if ins_task_idx is not None:
                # For now, assume non-local inputs are parametrised from the same
                # iteration as the upstream task:

                num_elems_per_iter = element_idx[ins_task_idx][
                    'num_elements_per_iteration'
                ]

                new_elems_idx = []
                for iter_idx in range(elem_idx_i['num_iterations']):
                    for i in inputs_idx['element_idx']:
                        new_elems_idx.append(
                            [j + (iter_idx * num_elems_per_iter) for j in i]
                        )
                element_idx[idx]['inputs'][input_alias]['element_idx'] = new_elems_idx

            else:
                # Copy local inputs' `inputs_idx` (local inputs must be the same for
                # all iterations):
                new_ins_idx = tile(inputs_idx['input_idx'], elem_idx_i['num_iterations'])
                element_idx[idx]['inputs'][input_alias]['input_idx'] = new_ins_idx

    return element_idx


def init_local_inputs(task_lst, is_from_file, check_integrity):
    """Normalise local inputs for each task.

    Parameters
    ----------
    task_lst : list of dict
    is_from_file : bool
        Has this task dict been loaded from a workflow file or is it associated
        with a brand new workflow?
    check_integrity : bool, optional
        Applicable if `is_from_file` is True. If True, re-generate `local_inputs`
        and compare them to those loaded from the file. If the equality test
        fails, raise. True by default. If False, `local_inputs` are still
        re-generated, but they are not compared to the loaded `local_inputs`.


    """

    for task_idx, task in enumerate(task_lst):

        if is_from_file:
            local_ins = task['local_inputs']
        else:
            local_ins = get_local_inputs(task, is_from_file)
            task_lst[task_idx]['local_inputs'] = local_ins

        # Select and set the correct command pathway index according to local inputs:
        loc_ins_vals = {}
        for in_name, in_vals_dict in local_ins['inputs'].items():
            loc_ins_vals.update({
                in_name: [in_vals_dict['vals'][i] for i in in_vals_dict['vals_idx']]
            })
        schema = task['schema']
        cmd_group = schema.command_group
        cmd_pth_idx = cmd_group.select_command_pathway(loc_ins_vals)
        task_lst[task_idx]['command_pathway_idx'] = cmd_pth_idx

        # Substitute command file names in input and output maps:
        command_file_names = cmd_group.get_command_file_names(cmd_pth_idx)
        for in_map_idx, in_map in enumerate(schema.input_map):
            for cmd_fn_label, cmd_fn in command_file_names['input_map'].items():
                if f'<<{cmd_fn_label}>>' in in_map['file']:
                    new_fn = in_map['file'].replace(f'<<{cmd_fn_label}>>', cmd_fn)
                    schema.input_map[in_map_idx]['file_raw'] = in_map['file']
                    schema.input_map[in_map_idx]['file'] = new_fn

        for out_map_idx, out_map in enumerate(schema.output_map):
            for out_map_file_idx, out_map_file in enumerate(out_map['files']):
                for cmd_fn_label, cmd_fn in command_file_names['output_map'].items():
                    if f'<<{cmd_fn_label}>>' in out_map_file['name']:
                        new_fn = out_map_file['name'].replace(
                            f'<<{cmd_fn_label}>>',
                            cmd_fn,
                        )
                        schema.output_map[out_map_idx]['files'][out_map_file_idx]['name_raw'] = out_map_file['name']
                        schema.output_map[out_map_idx]['files'][out_map_file_idx]['name'] = new_fn

    return task_lst


def validate_inputs(task_lst):
    """Validate supplied task inputs."""

    for task in task_lst:

        schema = task['schema']
        defined_inputs = task['local_inputs']['inputs'].keys()
        schema.check_surplus_inputs(defined_inputs)


def init_tasks(workflow, task_lst, is_from_file, num_iterations, check_integrity=True):
    """Construct and validate Task objects and the element indices
    from which to populate task inputs.

    Parameters
    ----------
    task_lst : list of dict
        List of task definitions. Each task dict can have the following
        keys:
            name
    is_from_file : bool
        If True, assume we are loading tasks from an HDF5 file, otherwise,
        the tasks are being constructed from an entirely new Workflow.
    check_integrity : bool, optional
        Applicable if `is_from_file=True`. It True, do some checks that the
        loaded information makes sense. True by default.

    Returns
    -------
    tasks : list of Task
    element_idx : list of dict

    Notes
    -----
    If loading an existing workflow, there will be additional keys in the
    dict elements of `task_lst`.

    """

    # Perform validation and initialisation that does not depend on other tasks:
    task_lst = [
        j
        for task_i in task_lst
        for j in validate_task_dict(
            task_i,
            is_from_file,
            Config.get('software'),
            Config.get('task_schemas'),
            Config.get('sources_maps'),
        )
    ]

    # Validate and normalise locally defined inputs:
    task_lst = init_local_inputs(task_lst, is_from_file, check_integrity=False)

    # Get dependencies, sort and add `task_idx` to each task:
    task_lst, dep_idx = order_tasks(task_lst)

    validate_inputs(task_lst)

    # Find element indices that determine the elements from which task inputs are drawn:
    element_idx = get_element_idx(task_lst, dep_idx, num_iterations)

    task_objs = []
    for task_idx, task_dict in enumerate(task_lst):

        if is_from_file:
            task_id = task_dict.pop('id')
            elements = task_dict.pop('elements')
        else:
            task_id = None
            num_elements = element_idx[task_idx]['num_elements']
            elements = [{'element_idx': elem_idx} for elem_idx in range(num_elements)]

        task = Task(workflow=workflow, **task_dict)
        task.init_elements(elements)

        if is_from_file:
            task.id = task_id
        else:
            task.generate_id()

        task_objs.append(task)

    return task_objs, element_idx, dep_idx
