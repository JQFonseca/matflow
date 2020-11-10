"""`matflow.models.command.py`

Module containing functionality for executing commands.

"""

import copy
from pathlib import Path, PureWindowsPath, PurePosixPath
from subprocess import run, PIPE
from pprint import pprint

import numpy as np

from matflow.errors import CommandError, TaskSchemaError
from matflow.utils import dump_to_yaml_string, get_specifier_dict
from matflow.hicklable import to_hicklable


def list_formatter(lst):
    return ' '.join([f'{i}' for i in lst])


DEFAULT_FORMATTERS = {
    str: lambda x: x,
    int: lambda number: str(number),
    float: lambda number: f'{number:.6f}',
    list: list_formatter,
    set: list_formatter,
    tuple: list_formatter,
}


class CommandGroup(object):
    """Class to represent a group of commands."""

    def __init__(self, all_commands, command_files=None, command_pathways=None):
        """
        Parameters
        ----------
        all_commands : list of Command objects
        command_files : dict, optional
        command_pathways : list of dict, optional

        """

        self.all_commands = [Command(**i) for i in all_commands]
        self.command_files = command_files or {}
        self.command_pathways = command_pathways

        self._validate_command_pathways()
        self._resolve_command_pathways()

    def __repr__(self):
        out = f'{self.__class__.__name__}(commands=['
        out += ', '.join([f'{i!r}' for i in self.all_commands]) + ']'
        out += ')'
        return out

    def __str__(self):
        return dump_to_yaml_string(self.as_dict())

    def as_dict(self):
        return to_hicklable(self)

    def check_pathway_conditions(self, inputs_list):
        """Check the command pathway conditions are compatible with a list of schema
        inputs.

        Parameters
        ----------
        inputs_list : list of str

        """

        for cmd_pth_idx, cmd_pth in enumerate(self.command_pathways):
            condition = cmd_pth.get('condition')
            if condition:
                bad_keys = set(condition) - set(inputs_list)
                if bad_keys:
                    bad_keys_fmt = ', '.join(['"{}"'.format(i) for i in bad_keys])
                    msg = ((f'Unknown command pathway condition inputs for command '
                            f'pathway index {cmd_pth_idx}: {bad_keys_fmt}.'))
                    raise CommandError(msg)

    def _validate_command_pathways(self):

        req_keys = ['commands_idx']
        allowed_keys = req_keys + ['condition', 'commands']

        # Check the condition list is a list of input labels for this task (have to be invoked by schema)
        no_condition_count = 0
        for cmd_pth_idx, cmd_pth in enumerate(self.command_pathways):

            bad_keys = set(cmd_pth) - set(allowed_keys)
            miss_keys = set(req_keys) - set(cmd_pth)

            if bad_keys:
                bad_keys_fmt = ', '.join(['"{}"'.format(i) for i in bad_keys])
                msg = ((f'Unknown command pathway keys for command pathway index '
                        f'{cmd_pth_idx}: {bad_keys_fmt}.'))
                raise CommandError(msg)

            if miss_keys:
                miss_keys_fmt = ', '.join(['"{}"'.format(i) for i in miss_keys])
                msg = (f'Missing required command pathway keys for command pathway '
                       f'index {cmd_pth_idx}: {miss_keys_fmt}.')
                raise CommandError(msg)

            if 'condition' not in cmd_pth:
                no_condition_count += 1

            cmds_idx = cmd_pth['commands_idx']
            num_cmds = len(self.all_commands)
            if (
                not isinstance(cmds_idx, list) or
                not all([i in range(num_cmds) for i in cmds_idx])
            ):
                msg = (f'`commands_idx` must be a list of integer indices into '
                       f'`all_commands` of length {num_cmds}, but received: '
                       f'"{cmds_idx}".')
                raise CommandError(msg)

        if no_condition_count > 1:
            msg = (f'Only one command pathway may be specified without a `condition` key '
                   f'(the default command pathway).')
            raise CommandError(msg)

    def _resolve_command_pathways(self):
        """Add a `commands` list to each `commands_pathway`, according to its 
        `commands_idx`."""

        for cmd_pth_idx, cmd_pth in enumerate(self.command_pathways):
            commands = [copy.deepcopy(self.all_commands[i])
                        for i in cmd_pth['commands_idx']]
            cmd_pth.update({'commands': commands})
            self._resolve_command_files(cmd_pth_idx)

    def _resolve_command_files(self, cmd_pathway_idx):

        # Validate command_files dict first:
        for cmd_fn_label, cmd_fn in self.command_files.items():
            if not isinstance(cmd_fn, str):
                msg = ('`command_files` must be a dict that maps a command file label to '
                       'a file name template')
                raise CommandError(msg)

        file_names = self.get_command_file_names(cmd_pathway_idx)

        for cmd_idx, command in enumerate(self.get_commands(cmd_pathway_idx)):

            for opt_idx, opt in enumerate(command.options):
                for opt_token_idx, opt_token in enumerate(opt):
                    options_files = file_names[cmd_idx]['options']
                    for cmd_fn_label, cmd_fn in options_files.items():
                        if f'<<{cmd_fn_label}>>' in opt_token:
                            new_fmt_opt = opt_token.replace(f'<<{cmd_fn_label}>>', cmd_fn)
                            command.options[opt_idx][opt_token_idx] = new_fmt_opt

            for param_idx, param in enumerate(command.parameters):
                params_files = file_names[cmd_idx]['parameters']
                for cmd_fn_label, cmd_fn in params_files.items():
                    if f'<<{cmd_fn_label}>>' in param:
                        new_param = param.replace(f'<<{cmd_fn_label}>>', cmd_fn)
                        command.parameters[param_idx] = new_param

            if command.stdin:
                stdin_files = file_names[cmd_idx]['stdin']
                for cmd_fn_label, cmd_fn in stdin_files.items():
                    if f'<<{cmd_fn_label}>>' in command.stdin:
                        new_stdin = command.stdin.replace(f'<<{cmd_fn_label}>>', cmd_fn)
                        command.stdin = new_stdin

            if command.stdout:
                new_stdout = command.stdout
                stdout_files = file_names[cmd_idx]['stdout']
                for cmd_fn_label, cmd_fn in stdout_files.items():
                    if f'<<{cmd_fn_label}>>' in command.stdout:
                        new_stdout = command.stdout.replace(f'<<{cmd_fn_label}>>', cmd_fn)
                        command.stdout = new_stdout

            if command.stderr:
                stderr_files = file_names[cmd_idx]['stderr']
                for cmd_fn_label, cmd_fn in stderr_files.items():
                    if f'<<{cmd_fn_label}>>' in command.stderr:
                        new_stderr = command.stderr.replace(f'<<{cmd_fn_label}>>', cmd_fn)
                        command.stderr = new_stderr

    def get_commands(self, cmd_pathway_idx):
        return self.command_pathways[cmd_pathway_idx]['commands']

    def select_command_pathway(self, inputs):
        """Get the correct command pathway index, give a set of input names and values.

        Parameters
        ----------
        inputs : dict of (str: list)
            Dict whose keys are input names and whose values are lists of input values
            (i.e. one element for each task sequence item).

        Returns
        -------
        cmd_pathway_idx : int

        """

        # Consider an input defined if any of its values (in the sequence) are not `None`:
        inputs_defined = [k for k, v in inputs.items() if any([i is not None for i in v])]

        # Sort pathways by most-specific first:
        order_idx = np.argsort([len(i.get('condition', []))
                                for i in self.command_pathways])[::-1]

        cmd_pathway_idx = None
        for cmd_pth_idx in order_idx:
            cmd_pth = self.command_pathways[cmd_pth_idx]
            condition = cmd_pth.get('condition', [])
            if not (set(condition) - set(inputs_defined)):
                # All inputs named in condition are defined
                cmd_pathway_idx = cmd_pth_idx
                break

        if cmd_pathway_idx is None:
            raise CommandError('Could not find suitable command pathway.')

        return cmd_pathway_idx

    def get_command_file_names(self, cmd_pathway_idx):

        def get_new_file_name(file_name_label):
            inc_fmt = str(file_name_increments[file_name_label])
            new_fn = self.command_files[file_name_label].replace('<<inc>>', inc_fmt)
            return new_fn

        file_names = []
        file_name_increments = {k: 0 for k in self.command_files.keys()}

        for command in self.get_commands(cmd_pathway_idx):

            file_names_i = {
                'stdin': {},
                'options': {},
                'parameters': {},
                'stdout': {},
                'stderr': {},
                'input_map': {},
                'output_map': {},
            }

            cmd_fn_is_incremented = {k: False for k in self.command_files.keys()}
            for cmd_fn_label in self.command_files.keys():

                new_fn = get_new_file_name(cmd_fn_label)

                # Input map, options, parameters and stdin should use the same increment.
                file_names_i['input_map'].update({cmd_fn_label: new_fn})

                for opt in command.options:
                    fmt_opt = list(opt)
                    for opt_token in fmt_opt:
                        if f'<<{cmd_fn_label}>>' in opt_token:
                            file_names_i['stdin'].update({cmd_fn_label: new_fn})

                for param in command.parameters:
                    if f'<<{cmd_fn_label}>>' in param:
                        file_names_i['parameters'].update({cmd_fn_label: new_fn})

                if command.stdin:
                    if f'<<{cmd_fn_label}>>' in command.stdin:
                        file_names_i['stdin'].update({cmd_fn_label: new_fn})

                # stdout, stderr and output map should use the next increment.
                if command.stdout:
                    if f'<<{cmd_fn_label}>>' in command.stdout:
                        file_name_increments[cmd_fn_label] += 1
                        cmd_fn_is_incremented[cmd_fn_label] = True
                        new_fn = get_new_file_name(cmd_fn_label)
                        file_names_i['stdout'].update({cmd_fn_label: new_fn})

                if command.stderr:
                    if f'<<{cmd_fn_label}>>' in command.stderr:
                        if not cmd_fn_is_incremented[cmd_fn_label]:
                            file_name_increments[cmd_fn_label] += 1
                            new_fn = get_new_file_name(cmd_fn_label)
                        file_names_i['stderr'].update({cmd_fn_label: new_fn})

                file_names_i['output_map'].update({cmd_fn_label: new_fn})

            file_names.append(file_names_i)

        return file_names

    def get_formatted_commands(self, inputs_list, num_cores, cmd_pathway_idx):
        """Format commands into strings with hpcflow variable substitutions where
        required.

        Parameters
        ----------
        inputs_list : list of str
            List of input names from which a subset of hpcflow variables may be defined.
        num_cores : int
            Number of CPU cores to use for this task. This is required to determine
            whether a "parallel_mode" should be included in the formatted commands.
        cmd_pathway_idx : int
            Which command pathway should be returned.

        Returns
        -------
        tuple of (fmt_commands, var_names)
            fmt_commands : list of dict
                Each list item is a dict that contains keys corresponding to an individual
                command to be run.
            var_names : dict of (str, str)
                A dict that maps a parameter name to an hpcflow variable name.

        """

        fmt_commands = []

        var_names = {}
        for command in self.get_commands(cmd_pathway_idx):

            fmt_opts = []
            for opt in command.options:
                fmt_opt = list(opt)
                for opt_token_idx, opt_token in enumerate(fmt_opt):
                    if opt_token in inputs_list:
                        # Replace with an `hpcflow` variable:
                        var_name = 'matflow_input_{}'.format(opt_token)
                        fmt_opt[opt_token_idx] = '<<{}>>'.format(var_name)
                        if opt_token not in var_names:
                            var_names.update({opt_token: var_name})

                fmt_opt_joined = ' '.join(fmt_opt)
                fmt_opts.append(fmt_opt_joined)

            fmt_params = []
            for param in command.parameters:

                fmt_param = param
                if param in inputs_list:
                    # Replace with an `hpcflow` variable:
                    var_name = 'matflow_input_{}'.format(param)
                    fmt_param = '<<{}>>'.format(var_name)

                    if param not in var_names:
                        var_names.update({param: var_name})

                fmt_params.append(fmt_param)

            cmd_fmt = ' '.join([command.command] + fmt_opts + fmt_params)

            if command.stdin:
                cmd_fmt += ' < {}'.format(command.stdin)

            if command.stdout:
                cmd_fmt += ' >> {}'.format(command.stdout)

            if command.stderr:
                if command.stderr == command.stdout:
                    cmd_fmt += ' 2>&1'
                else:
                    cmd_fmt += ' 2>> {}'.format(command.stderr)

            cmd_dict = {'line': cmd_fmt}
            if command.parallel_mode and num_cores > 1:
                cmd_dict.update({'parallel_mode': command.parallel_mode})

            fmt_commands.append(cmd_dict)

        return (fmt_commands, var_names)


class Command(object):
    'Class to represent a command to be executed by a shell.'

    def __init__(self, command, software, options=None, parameters=None, stdin=None,
                 stdout=None, stderr=None, parallel_mode=None, input_map=None,
                 output_map=None):

        self.command = command
        self.software = software
        self.parallel_mode = parallel_mode

        self.options = options or []
        self.parameters = parameters or []
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr

        self.input_map = input_map or []
        self.output_map = output_map or []

    def __repr__(self):
        out = f'{self.__class__.__name__}({self.command!r}, software={self.software!r}'
        if self.options:
            out += f', options={self.options!r}'
        if self.parameters:
            out += f', parameters={self.parameters!r}'
        if self.stdin:
            out += f', stdin={self.stdin!r}'
        if self.stdout:
            out += f', stdout={self.stdout!r}'
        if self.stderr:
            out += f', stderr={self.stderr!r}'
        out += ')'
        return out

    def __str__(self):

        cmd_fmt = ' '.join(
            [self.command] +
            [' '.join(i) for i in self.options] +
            self.parameters
        )

        if self.stdin:
            cmd_fmt += ' < {}'.format(self.stdin)
        if self.stdout:
            cmd_fmt += ' > {}'.format(self.stdout)
        if self.stderr:
            if self.stderr == self.stdout:
                cmd_fmt += ' 2>&1'
            else:
                cmd_fmt += ' 2> {}'.format(self.stderr)

        return cmd_fmt

    def validate_input_map(self, schema_name, cmd_idx, input_aliases):

        err = f'Validation failed for command {cmd_idx} in task schema "{schema_name}". '

        # Check correct keys in supplied input/output maps:
        for in_map_idx, in_map in enumerate(self.input_map):

            req_keys = ['inputs', 'file']
            allowed_keys = set(req_keys + ['save', 'file_initial'])
            miss_keys = list(set(req_keys) - set(in_map.keys()))
            bad_keys = list(set(in_map.keys()) - allowed_keys)

            msg = (f'Input maps must map a list of `inputs` into a `file` (with an '
                   f'optional `save` key).')
            if miss_keys:
                miss_keys_fmt = ', '.join(['"{}"'.format(i) for i in miss_keys])
                raise TaskSchemaError(err + msg + f' Missing keys are: {miss_keys_fmt}.')
            if bad_keys:
                bad_keys_fmt = ', '.join(['"{}"'.format(i) for i in bad_keys])
                raise TaskSchemaError(err + msg + f' Unknown keys are: {bad_keys_fmt}.')

            if not isinstance(in_map['inputs'], list):
                msg = 'Input map `inputs` must be a list.'
                raise TaskSchemaError(err + msg)

        input_map_ins = [j for i in self.input_map for j in i['inputs']]
        unknown_map_inputs = set(input_map_ins) - set(input_aliases)

        if unknown_map_inputs:
            bad_ins_map_fmt = ', '.join(['"{}"'.format(i) for i in unknown_map_inputs])
            msg = (f'Input map inputs {bad_ins_map_fmt} not known by the schema with '
                   f'input (aliases): {input_aliases}.')
            raise TaskSchemaError(err + msg)

    def validate_output_map(self, schema_name, cmd_idx, outputs):

        err = f'Validation failed for command {cmd_idx} in task schema "{schema_name}". '

        out_map_opt_names = []
        for out_map_idx, out_map in enumerate(self.output_map):

            req_keys = ['files', 'output']
            allowed_keys = set(req_keys + ['options'])
            miss_keys = list(set(req_keys) - set(out_map.keys()))
            bad_keys = list(set(out_map.keys()) - allowed_keys)

            msg = (f'Output maps must map a list of `files` into an `output` (with '
                   f'optional `options`). ')
            if miss_keys:
                miss_keys_fmt = ', '.join(['"{}"'.format(i) for i in miss_keys])
                raise TaskSchemaError(err + msg + f'Missing keys are: {miss_keys_fmt}.')

            if bad_keys:
                bad_keys_fmt = ', '.join(['"{}"'.format(i) for i in bad_keys])
                raise TaskSchemaError(err + msg + f'Unknown keys are: {bad_keys_fmt}.')

            if not isinstance(out_map['output'], str):
                msg = 'Output map `output` must be a string.'
                raise TaskSchemaError(err + msg)

            for out_map_file_idx, out_map_file in enumerate(out_map['files']):
                if ('name' not in out_map_file) or ('save' not in out_map_file):
                    msg = (f'Specify keys `name` (str) and `save` (bool) in output map '
                           f'`files` key.')
                    raise TaskSchemaError(err + msg)

            # Normalise and check output map options:
            out_map_opts = out_map.get('options', [])
            if out_map_opts:
                if not isinstance(out_map_opts, list):
                    msg = (
                        f'If specified, output map options should be a list, but the '
                        f'following was specified: {out_map_opts}.'
                    )
                    raise TaskSchemaError(err + msg)

            for out_map_opt_idx, out_map_opt_i in enumerate(out_map_opts):

                opts = get_specifier_dict(out_map_opt_i, name_key='name')
                req_opts_keys = ['name']
                allowed_opts_keys = req_opts_keys + ['default']
                bad_opts_keys = list(set(opts.keys()) - set(allowed_opts_keys))
                miss_opts_keys = list(set(req_opts_keys) - set(opts.keys()))

                if bad_opts_keys:
                    bad_opts_keys_fmt = ', '.join([f'"{i}"' for i in bad_opts_keys])
                    msg = (
                        f'Unknown output map option keys for output map index '
                        f'{out_map_idx} and output map option index {out_map_opt_idx}: '
                        f'{bad_opts_keys_fmt}. Allowed keys are: {allowed_opts_keys}.'
                    )
                    raise TaskSchemaError(err + msg)

                if miss_opts_keys:
                    miss_opts_keys_fmt = ', '.join([f'"{i}"' for i in miss_opts_keys])
                    msg = (
                        f'Missing output map option keys for output map index '
                        f'{out_map_idx} and output map option index {out_map_opt_idx}: '
                        f'{miss_opts_keys_fmt}.'
                    )
                    raise TaskSchemaError(err + msg)

                if opts['name'] in out_map_opt_names:
                    msg = (
                        f'Output map options must be uniquely named across all output '
                        f'maps of a given task schema, but the output map option '
                        f'"{opts["name"]}" is repeated.'
                    )
                    raise TaskSchemaError(err + msg)
                else:
                    out_map_opt_names.append(opts['name'])

                self.output_map[out_map_idx]['options'][out_map_opt_idx] = opts

        output_map_outs = [i['output'] for i in self.output_map]
        unknown_map_outputs = set(output_map_outs) - set(outputs)

        if unknown_map_outputs:
            bad_outs_map_fmt = ', '.join(['"{}"'.format(i) for i in unknown_map_outputs])
            msg = (f'Output map outputs {bad_outs_map_fmt} not known by the schema with '
                   f'outputs: {outputs}.')
            raise TaskSchemaError(err + msg)
