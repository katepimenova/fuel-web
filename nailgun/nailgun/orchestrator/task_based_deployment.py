# -*- coding: utf-8 -*-

#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import copy
from distutils.version import StrictVersion
import itertools

import six

from nailgun import consts
from nailgun.errors import errors
from nailgun.logger import logger
from nailgun.orchestrator import plugins_serializers
from nailgun.orchestrator.tasks_serializer import CreateVMsOnCompute
from nailgun.orchestrator.tasks_serializer import StandartConfigRolesHook
from nailgun.orchestrator.tasks_serializer import TaskSerializers
from nailgun.orchestrator.tasks_templates import make_noop_task
from nailgun.utils.role_resolver import NameMatchingPolicy
from nailgun.utils.role_resolver import NullResolver
from nailgun.utils.role_resolver import RoleResolver


class NoopSerializer(StandartConfigRolesHook):
    """Serializes tasks that should be skipped by astute."""
    def should_execute(self):
        return True

    def get_uids(self):
        groups = self.task.get('groups', self.task.get('role'))
        if groups is None:
            # it means that task is not associated with any node
            return [None]
        return self.role_resolver.resolve(groups)

    def serialize(self):
        uids = self.get_uids()
        yield make_noop_task(uids, self.task)


class PluginTaskSerializer(StandartConfigRolesHook):
    serializer_class = None

    def should_execute(self):
        return True

    def serialize(self):
        serializer = self.serializer_class(
            self.cluster, self.nodes, role_resolver=self.role_resolver
        )
        return itertools.chain(
            serializer.serialize_begin_tasks(),
            serializer.serialize_end_tasks())


class PluginPreDeploymentSerializer(PluginTaskSerializer):
    """Serializes plugin pre-deployment tasks."""
    serializer_class = \
        plugins_serializers.PluginsPreDeploymentHooksSerializer


class PluginPostDeploymentSerializer(PluginTaskSerializer):
    """Serializes plugin post-deployment tasks."""
    serializer_class = \
        plugins_serializers.PluginsPostDeploymentHooksSerializer


def add_plugin_deployment_hooks(tasks):
    """Adds the artificial tasks for deployment hooks from plugins.

    :param tasks: the origin list of deployment tasks
    :return: the sequence of deployment tasks that includes
             pre/post deployment hooks for plugins
    """

    # TODO(bgaifullin): Make this tasks in plugins as obsolete
    # and drop support of them
    # of Task Based Deployment
    # Added fake task for pre and post.
    # This will cause engine to generate chain of tasks for each stage.
    # Tasks in chain will run step by step.

    hooks = [
        {'id': consts.PLUGIN_PRE_DEPLOYMENT_HOOK,
         'version': consts.TASK_CROSS_DEPENDENCY,
         'type': consts.PLUGIN_PRE_DEPLOYMENT_HOOK,
         'requires': [consts.STAGES.pre_deployment + '_end'],
         'required_for': [consts.STAGES.deploy + '_start']},
        {'id': consts.PLUGIN_POST_DEPLOYMENT_HOOK,
         'version': consts.TASK_CROSS_DEPENDENCY,
         'type': consts.PLUGIN_POST_DEPLOYMENT_HOOK,
         'requires': [consts.STAGES.post_deployment + '_end']}
    ]

    return itertools.chain(iter(tasks), iter(hooks))


class DeployTaskSerializer(TaskSerializers):
    task_types_mapping = {
        consts.ORCHESTRATOR_TASK_TYPES.skipped: NoopSerializer,
        consts.ORCHESTRATOR_TASK_TYPES.stage: NoopSerializer,
        consts.PLUGIN_PRE_DEPLOYMENT_HOOK: PluginPreDeploymentSerializer,
        consts.PLUGIN_POST_DEPLOYMENT_HOOK: PluginPostDeploymentSerializer
    }

    def __init__(self):
        # because we are used only stage_serializers need to
        # add CreateVMsOnCompute serializer to stage_serializers
        # deploy_serializers shall be empty
        super(DeployTaskSerializer, self).__init__(
            TaskSerializers.stage_serializers + [CreateVMsOnCompute], []
        )

    def get_stage_serializer(self, task):
        serializer = self.task_types_mapping.get(task['type'], None)
        if serializer is not None:
            return serializer
        return super(DeployTaskSerializer, self).get_stage_serializer(
            task
        )


class TaskProcessor(object):
    """Helper class for deal with task chains."""

    task_attributes_to_copy = (
        'requires', 'cross-depends',
        'required_for', 'cross-depended-by'
    )

    min_supported_task_version = StrictVersion(consts.TASK_CROSS_DEPENDENCY)

    def __init__(self):
        # stores mapping between ids of generated tasks and origin task
        self.origin_task_ids = dict()

    @classmethod
    def ensure_task_based_deploy_allowed(cls, task):
        """Raises error if task does not support cross-dependencies.

        :param task: the task instance
        :raises: errors.TaskBaseDeploymentNotAllowed
        """
        if task.get('type') == consts.ORCHESTRATOR_TASK_TYPES.stage:
            return

        task_version = StrictVersion(task.get('version', '1.0.0'))
        if task_version < cls.min_supported_task_version:
            logger.warning(
                "Task '%s' does not supported task based deploy.",
                task['id']
            )
            raise errors.TaskBaseDeploymentNotAllowed

    def get_origin(self, task_id):
        """Gets the origin ID of task.

        :param task_id: the id of task
        """
        return self.origin_task_ids.get(task_id, task_id)

    def process_tasks(self, origin_task, serialized_tasks):
        """Processes serialized tasks.

        Adds ID for each task, because serialized task does not contain it.
        Adds the proper links between serialized tasks in case if one
        library task expands to several astute tasks.

        :param origin_task: the origin task object
        :param serialized_tasks: the sequence of serialized tasks
        :return: the sequence of astute tasks
        """

        # TODO(bgaifullin): remove expanding library tasks from nailgun
        # The next task serializers (UploadMOSRepo,UploadConfiguration)
        # generates a chain of several tasks from one library task
        # in this case we need to generate proper dependencies between
        # the tasks in this chain.
        # the first task shall have only "requires" and "cross-depends"
        # each task in chain shall have only link to previous
        # task in same chain
        # the last task also shall have the "required_for" and
        # "cross-depended-by" fields.
        # as result we have next chain of task
        # scheme for chain:
        # [requires]
        #     ^
        # task_start
        #     ^
        #  task#1
        #     ^
        #    ...
        #     ^
        #  task#N
        #     ^
        #  task_end
        #     ^
        # [required_for]

        task_iter = iter(serialized_tasks)
        frame = collections.deque(itertools.islice(task_iter, 2), maxlen=2)

        # in case if there is no nodes was resolved
        # the serializers return empty list of task
        if len(frame) == 0:
            return

        # check only if task will be add to graph
        self.ensure_task_based_deploy_allowed(origin_task)

        if len(frame) == 1:
            # It is simple case when chain contains only 1 task
            # do nothing
            # check that that frame is not empty
            yield self._convert_task(frame.pop(), origin_task)
            return

        # it is chain of tasks, need to properly handle them
        logger.debug("the chain detected: %s", origin_task['id'])
        task = self._convert_first_task(frame.popleft(), origin_task)
        # the client can modify original task
        # always return the copy, need to save only structure
        # and the shallow copy will be enough
        yield task.copy()

        # uses counter to generate ids for tasks in chain
        for n in itertools.count(1):
            try:
                frame.append(next(task_iter))
            except StopIteration:
                break
            next_task = self._convert_to_chain_task(
                frame.popleft(), origin_task, n
            )
            # link current task with previous
            self._link_tasks(task, next_task)
            task = next_task
            # return shallow copy, see commend above
            yield task.copy()

        next_task = self._convert_last_task(frame.pop(), origin_task)
        # link this task with previous in chain
        self._link_tasks(task, next_task)
        yield next_task

    def _convert_first_task(self, serialized, origin):
        """Make the first task in chain.

        :param serialized: the serialized task instance
        :param origin: the origin puppet task
        :returns: the patched serialized task
        """

        # first task shall contains only requires and cross-depends
        # see comment in def process
        return self._convert_task(
            serialized,
            origin,
            self.get_first_task_id(origin['id']),
            ('requires', 'cross-depends')
        )

    def _convert_to_chain_task(self, serialized, origin, num):
        """Make the first task in chain.

        :param serialized: the serialized task instance
        :param origin: the origin puppet task
        :param num: the task number in chain
        :returns: the patched serialized task
        """
        # do no copy relation attributes
        # see comment in def process
        return self._convert_task(
            serialized, origin,
            self.get_task_id(origin['id'], num),
            ()
        )

    def _convert_last_task(self, serialized, origin):
        """Patches last astute  task in chain.

        :param serialized: the serialized task instance
        :param origin: the origin puppet task
        :returns: the patched serialized task
        """

        # last task shall contains only required_for and cross-depended-by
        # see comment in def process
        return self._convert_task(
            serialized,
            origin,
            self.get_last_task_id(origin['id']),
            ('required_for', 'cross-depended-by')
        )

    def _convert_task(self, serialized, origin, task_id=None, attrs=None):
        """Make the astute task.

        Note: the serialized will be modified.

        :param serialized: the serialized task instance
        :param origin: the origin puppet task
        :param task_id: the task id
        :param attrs: the attributes that will be copied from puppet task
        :returns: the patched serialized task
        """
        if attrs is None:
            attrs = self.task_attributes_to_copy
        if task_id is None:
            task_id = origin['id']
        serialized['id'] = task_id
        for attr in attrs:
            try:
                serialized[attr] = origin[attr]
            except KeyError:
                pass
        self.origin_task_ids[task_id] = origin['id']
        return serialized

    @staticmethod
    def _link_tasks(previous, current):
        """Link the previous and current task in chain.

        :param previous: the previous task instance
        :param current: the current task instance
        """

        # in case if uuis is same, that means task will run on same nodes
        if previous.get('uids') == current.get('uids'):
            logger.debug(
                "connect task '%s' with previous in chain '%s'",
                current['id'], previous['id']
            )
            current.setdefault('requires', []).append(previous['id'])
        else:
            # the list of nodes is different, make cross-depends
            logger.debug(
                "cross node dependencies: task '%s', previous task '%s', "
                "nodes: %s",
                current['id'], previous['id'],
                ', '.join(previous.get('uids', ()))
            )
            requires_ex = current.setdefault('requires_ex', [])
            for node_id in previous.get('uids', ()):
                requires_ex.append(
                    {'name': previous['id'], 'node_id': node_id}
                )

    @staticmethod
    def get_task_id(chain_name, num):
        return "{0}#{1}".format(chain_name, num)

    @staticmethod
    def get_first_task_id(chain_name):
        return chain_name + "_start"

    @staticmethod
    def get_last_task_id(chain_name):
        return chain_name + "_end"


class TasksSerializer(object):
    """The deploy tasks serializer."""

    def __init__(self, cluster, nodes, task_ids=None):
        """Initializes.

        :param cluster: Cluster instance
        :param nodes: the sequence of nodes for deploy
        :param task_ids: Only specified tasks will be executed,
                         If None, all tasks will be executed
        """
        self.cluster = cluster
        self.role_resolver = RoleResolver(nodes)
        self.task_serializer = DeployTaskSerializer()
        self.task_processor = TaskProcessor()
        self.tasks_per_node = collections.defaultdict(dict)
        self.task_filter = self.make_task_filter(task_ids)

    @classmethod
    def serialize(cls, cluster, nodes, tasks, task_ids=None):
        """Resolves roles and dependencies for tasks.

        :param cluster: the cluster instance
        :param nodes: the list of nodes
        :param tasks: the list of tasks
        :param task_ids: Only specified tasks will be executed,
                         If None, all tasks will be executed
        :return: the list of serialized task per node
        """
        serializer = cls(cluster, nodes, task_ids)
        serializer.resolve_nodes(add_plugin_deployment_hooks(tasks), nodes)
        serializer.resolve_dependencies()
        return dict(
            (k, list(six.itervalues(v)))
            for k, v in six.iteritems(serializer.tasks_per_node)
        )

    def resolve_nodes(self, tasks, nodes):
        """Resolves node roles in tasks.

        :param tasks: the deployment tasks
        :param nodes: the list of nodes to deploy
        :return the mapping tasks per node
        """

        tasks_mapping = dict()
        groups = list()

        for task in tasks:
            if task.get('type') == consts.ORCHESTRATOR_TASK_TYPES.group:
                groups.append(task)
            else:
                tasks_mapping[task['id']] = task
                self.process_task(
                    task, nodes, lambda _: self.role_resolver,
                    skip=not self.task_filter(task['id'])
                )

        self.expand_task_groups(groups, tasks_mapping)
        # make sure that null node is present
        self.tasks_per_node.setdefault(None, dict())

    def process_task(self, task, nodes, resolver_factory, skip=False):
        """Processes one task one nodes of cluster.

        :param task: the task instance
        :param nodes: the list of nodes
        :param resolver_factory: the factory creates role-resolver
        :param skip: make the task as skipped
        """

        serializer_factory = self.task_serializer.get_stage_serializer(
            task
        )
        task_serializer = serializer_factory(
            task, self.cluster, nodes, role_resolver=resolver_factory(nodes)
        )
        # do not pass skipped attribute to astute
        skipped = skip or task.pop('skipped', False) or \
            not task_serializer.should_execute()
        for astute_task in self.task_processor.process_tasks(
                task, task_serializer.serialize()):
            # all skipped task shall have type skipped
            # do not exclude them from graph to keep connections between nodes
            if skipped:
                astute_task['type'] = \
                    consts.ORCHESTRATOR_TASK_TYPES.skipped

            for node_id in astute_task.pop('uids', ()):
                node_tasks = self.tasks_per_node[node_id]
                # de-duplication the tasks on node
                # since task can be added after expand group need to
                # overwrite if existed task is skipped and new is not skipped.
                if self.need_update_task(node_tasks, astute_task):
                    node_tasks[astute_task['id']] = copy.deepcopy(astute_task)

    def resolve_dependencies(self):
        """Resolves tasks dependencies."""

        for node_id, tasks in six.iteritems(self.tasks_per_node):
            for task in six.itervalues(tasks):
                task['requires'] = list(
                    self.expand_dependencies(
                        node_id, task.get('requires'), False
                    )
                )
                task['required_for'] = list(
                    self.expand_dependencies(
                        node_id, task.get('required_for'), True
                    )
                )
                task['requires'].extend(
                    self.expand_cross_dependencies(
                        node_id, task.pop('cross-depends', None), False
                    )
                )

                task['required_for'].extend(
                    self.expand_cross_dependencies(
                        node_id, task.pop('cross-depended-by', None), True
                    )
                )
                task['requires'].extend(task.pop('requires_ex', ()))
                task['required_for'].extend(task.pop('required_for_ex', ()))

    def expand_task_groups(self, groups, task_mapping):
        """Expand group of tasks.

        :param groups: the all tasks with type 'group'
        :param task_mapping: the mapping task id to task object
        """
        for task in groups:
            skipped = not self.task_filter(task['id'])
            node_ids = self.role_resolver.resolve(task.get('role', ()))
            for sub_task_id in task.get('tasks', ()):
                try:
                    sub_task = task_mapping[sub_task_id]
                except KeyError:
                    raise errors.InvalidData(
                        'Task %s cannot be resolved', sub_task_id
                    )

                # if group is not excluded, all task should be run as well
                # otherwise check each task individually
                self.process_task(
                    sub_task, node_ids, NullResolver,
                    skip=skipped and not self.task_filter(sub_task_id)
                )

    def expand_dependencies(self, node_id, dependencies, is_required_for):
        """Expands task dependencies on same node.

        :param node_id: the ID of target node
        :param dependencies: the list of dependencies on same node
        :param is_required_for: means task from required_for section
        """
        if not dependencies:
            return

        # need to search dependencies on node and in sync points
        node_ids = [node_id, None]
        for name in dependencies:
            for rel in self.resolve_relation(name, node_ids, is_required_for):
                yield rel

    def expand_cross_dependencies(
            self, node_id, dependencies, is_required_for):
        """Expands task dependencies on same node.

        :param node_id: the ID of target node
        :param dependencies: the list of cross-node dependencies
        :param is_required_for: means task from required_for section
        """
        if not dependencies:
            return

        for dep in dependencies:
            roles = dep.get('role', consts.TASK_ROLES.all)

            if roles == consts.TASK_ROLES.self:
                node_ids = [node_id]
            else:
                node_ids = self.role_resolver.resolve(
                    roles, dep.get('policy', consts.NODE_RESOLVE_POLICY.all)
                )
            relations = self.resolve_relation(
                dep['name'], node_ids, is_required_for
            )
            for rel in relations:
                yield rel

    def resolve_relation(self, name, node_ids, is_required_for):
        """Resolves the task relation.

        :param name: the name of task
        :param node_ids: the ID of nodes where need to search
        :param is_required_for: means task from required_for section
        """
        match_policy = NameMatchingPolicy.create(name)
        for node_id in node_ids:
            applied_tasks = set()
            for task_name in self.tasks_per_node[node_id]:
                if task_name == name:
                    # the simple case when name of current task
                    # is exact math to name of task that is search
                    yield {"name": task_name, "node_id": node_id}
                    continue

                # at first get the original task name, actual
                # when current task is part of chain
                original_task = self.task_processor.get_origin(task_name)
                if original_task in applied_tasks or \
                        not match_policy.match(original_task):
                    continue

                applied_tasks.add(original_task)
                if original_task is not task_name:
                    if is_required_for:
                        task_name_gen = self.task_processor.get_first_task_id
                    else:
                        task_name_gen = self.task_processor.get_last_task_id
                    task_name = task_name_gen(original_task)

                yield {"name": task_name, "node_id": node_id}

    @classmethod
    def need_update_task(cls, tasks, task):
        """Checks that task shall overwrite existed one or should be added.

        :param tasks: the current node tasks
        :param task: the astute task object
        :return True if task is not present or must be overwritten
                otherwise False
        """
        existed_task = tasks.get(task['id'])
        if existed_task is None:
            return True

        if existed_task['type'] == task['type']:
            return False

        return task['type'] != consts.ORCHESTRATOR_TASK_TYPES.skipped

    @classmethod
    def make_task_filter(cls, task_ids):
        """Makes task filter according to specified ids.

        :param task_ids: the selected  ids of tasks
        :return: function that check task
        """
        if not task_ids:
            return lambda _: True

        if not isinstance(task_ids, set):
            task_ids = set(task_ids)

        return lambda task_id: task_id in task_ids
