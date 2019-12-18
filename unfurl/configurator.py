import six
import collections
import re
import os
from .support import Status, Defaults, ResourceChanges
from .result import serializeValue, ChangeAware, Results, ResultsMap
from .util import (
    AutoRegisterClass,
    lookupClass,
    validateSchema,
    findSchemaErrors,
    UnfurlError,
    UnfurlTaskError,
    UnfurlAddingResourceError,
)
from .eval import Ref, mapValue, RefContext
from ruamel.yaml import YAML

yaml = YAML()

import logging

logger = logging.getLogger("unfurl")


class ConfigOp(object):
    """
The operations defined in unfurl.interfaces.Configure
    """

    @staticmethod
    def toStandardOp(op):
        return dict(add="create", update="configure", remove="delete").get(op)


for op in "add update remove discover check".split():
    setattr(ConfigOp, op, op)

for op in "create configure start stop delete".split():
    setattr(ConfigOp, op, op)


class Environment(object):
    def __init__(self, vars=None, isolate=False, passvars=None, addinputs=False, **kw):
        """
        environment:
          isolate: true
          addinputs: true
          passvars:
            - ANSIBLE_VERBOSITY
            - UNFURL_LOGGING
            - ANDROID_*
          vars:
            FOO: "{{}}"
      """
        self.vars = vars or {}
        self.isolate = isolate
        self.passvars = passvars
        self.addinputs = addinputs

    # XXX add default passvars:
    # see https://tox.readthedocs.io/en/latest/config.html#tox-environment-settings list of default passenv
    # also SSH_AUTH_SOCK for ssh_agent
    def getSystemVars(self):
        # this need to execute on the operation_host the task is running on!
        if self.isolate:
            if self.passvars:  # XXX support glob, support UNFURL_PASSENV
                env = {k: v for k, v in os.environ.items() if k in self.passvars}
            else:
                env = {}
        else:
            env = os.environ.copy()
        return env

    def __eq__(self, other):
        if not isinstance(other, Environment):
            return False
        return (
            self.vars == other.vars
            and self.isolate == other.isolate
            and self.passvars == other.passvars
            and self.addinputs == other.addinputs
        )


# we want ConfigurationSpec to be standalone and easily serializable
class ConfigurationSpec(object):
    @classmethod
    def getDefaults(self):
        return dict(
            className=None,
            majorVersion=0,
            minorVersion="",
            workflow=Defaults.workflow,
            timeout=None,
            environment=None,
            inputs=None,
            inputSchema=None,
            preConditions=None,
            postConditions=None,
            installer=None,
        )

    def __init__(
        self,
        name,
        operation,
        className=None,
        majorVersion=0,
        minorVersion="",
        workflow=Defaults.workflow,
        timeout=None,
        environment=None,
        inputs=None,
        inputSchema=None,
        preConditions=None,
        postConditions=None,
        installer=None,
    ):
        assert name and className, "missing required arguments"
        self.name = name
        self.operation = operation
        self.className = className
        self.majorVersion = majorVersion
        self.minorVersion = minorVersion
        self.workflow = workflow
        self.timeout = timeout
        self.environment = Environment(**(environment or {}))
        self.inputs = inputs or {}
        self.inputSchema = inputSchema
        self.preConditions = preConditions
        self.postConditions = postConditions
        self.installer = installer

    def findInvalidateInputs(self, inputs):
        if not self.inputSchema:
            return []
        return findSchemaErrors(serializeValue(inputs), self.inputSchema)

    # XXX same for postConditions
    def findInvalidPreconditions(self, target):
        if not self.preConditions:
            return []
        # XXX this should be like a Dependency object
        expanded = serializeValue(target.attributes)
        return findSchemaErrors(expanded, self.preConditions)

    def create(self):
        # XXX2 throw clearer exception if couldn't load class
        return lookupClass(self.className)(self)

    def shouldRun(self):
        return Defaults.shouldRun

    def copy(self, **mods):
        args = self.__dict__.copy()
        args.update(mods)
        return ConfigurationSpec(**args)

    def __eq__(self, other):
        if not isinstance(other, ConfigurationSpec):
            return False
        # XXX3 add unit tests
        return (
            self.name == other.name
            and self.operation == other.operation
            and self.className == other.className
            and self.majorVersion == other.majorVersion
            and self.minorVersion == other.minorVersion
            and self.workflow == other.workflow
            and self.timeout == other.timeout
            and self.environment == other.environment
            and self.inputs == other.inputs
            and self.inputSchema == self.inputSchema
            and self.preConditions == other.preConditions
            and self.postConditions == other.postConditions
        )


class ConfiguratorResult(object):
    """
  If applied is True,
  the current pending configuration is set to the effective, active one
  and the previous configuration is no longer in effect.

  Modified indicates whether the underlying state of configuration,
  was changed i.e. the physically altered the system this configuration represents.

  Readystate reports the Status of the current configuration.
  """

    def __init__(
        self,
        applied,
        modified,
        status=None,
        configChanged=None,
        result=None,
        success=None,
        outputs=None,
        exception=None,
    ):
        self.applied = applied
        self.modified = modified
        self.readyState = status
        self.configChanged = configChanged
        self.result = result
        self.success = success
        self.outputs = outputs
        self.exception = None

    def __str__(self):
        result = "" if self.result is None else str(self.result)[:240] + "..."
        return (
            "changes: "
            + (
                " ".join(
                    filter(
                        None,
                        [
                            self.success and "success",
                            self.modified and "modified",
                            self.readyState and self.readyState.name,
                        ],
                    )
                )
                or "none"
            )
            + "\n   "
            + result
        )


@six.add_metaclass(AutoRegisterClass)
class Configurator(object):
    def __init__(self, configurationSpec):
        self.configSpec = configurationSpec

    def getGenerator(self, task):
        return self.run(task)

    # yields a JobRequest, TaskRequest or a ConfiguratorResult
    def run(self, task):
        yield task.createResult(False, False)

    def canDryRun(self, task):
        """
        Called when dry run call.
        If a configurator supports dry-run it should return True here and make sure it checks whether `task.dryRun` in run.
        """
        return False

    def cantRun(self, task):
        """
    Does this configurator support the requested action and parameters
    given the current state of the resource?
    (e.g. can we upgrade from the previous configuration?)

    Returns False or an error message (list or string)
    """
        return False

    def shouldRun(self, task):
        """Does this configuration need to be run?"""
        return self.configSpec.shouldRun()

    # XXX3 should be called during when checking dependencies
    # def checkConfigurationStatus(self, task):
    #   """Is this configuration still valid?"""
    #   return Status.ok


class TaskView(object):
    """
  The interface presented to configurators.
  """

    def __init__(self, manifest, configSpec, target, reason=None, dependencies=None):
        # public:
        self.configSpec = configSpec
        self.target = target
        self.reason = reason
        self.logger = logger
        # XXX refcontext should include TARGET HOST etc.
        # private:
        self._inputs = None
        self._environ = None
        self._manifest = manifest
        self.messages = []
        self._addedResources = []
        self._dependenciesChanged = False
        self.dependencies = dependencies or {}
        self._resourceChanges = ResourceChanges()

    @property
    def inputs(self):
        """
        Exposes inputs and task settings as expression variables, so they can be accessed like:

        eval: $inputs::param

        or in jinja2 templates:

        {{ inputs.param }}
        """
        if self._inputs is None:
            # XXX should ConfigTask be full ResourceRef so we can have live view of status etc.?
            # this way we could enable resumable pending tasks (could save state in operation results)
            inputs = self.configSpec.inputs.copy()
            vars = dict(inputs=inputs, task=self.getSettings())
            # expose inputs lazily to allow self-referencee
            self._inputs = ResultsMap(inputs, RefContext(self.target, vars))
        return self._inputs

    @property
    def environ(self):
        if self._environ is None:
            env = self.configSpec.environment.getSystemVars()
            specvars = serializeValue(
                mapValue(self.configSpec.environment.vars, self.inputs.context),
                resolveExternal=True,
            )
            if self.configSpec.environment.addinputs:
                env.update(serializeValue(self.inputs), resolveExternal=True)
            # XXX validate that all vars are bytes or string (json serialize if not?)
            env.update(specvars)
            self._environ = env

        return self._environ

    def getSettings(self):
        return dict(
            verbose=self.verbose,
            name=self.configSpec.name,
            dryRun=self.dryRun,
            workflow=self.configSpec.workflow,
            operation=self.configSpec.operation,
            timeout=self.configSpec.timeout,
            target=self.target.name,
        )

    def addMessage(self, message):
        self.messages.append(message)

    def findResource(self, name):
        return self._manifest.getRootResource().findResource(name)

    # XXX
    # def pending(self, modified=None, sleep=100, waitFor=None, outputs=None):
    #     """
    #     >>> yield task.pending(60)
    #
    #     set modified to True to advise that target has already been modified
    #
    #     outputs to share operation outputs so far
    #     """

    def done(
        self,
        success,
        modified=None,
        status=None,
        result=None,
        outputs=None,
        captureException=None,
    ):
        """
        `run()` should call this method and yield its return value before terminating.

        >>> yield task.done(True)

        `success`  indicates if this operation completed without an error.
        `modified` indicates that the physical instance was modified by this operation.
        `status`   should be set if the operation changed the operational status of the target instance.
                   If not specified, the runtime will updated the instance status as needed, based
                   the operation preformed and observed changes to the instance (attributes changed).
        `result`   A dictionary that will be serialized as YAML into the changelog, can contain any useful data about these operation.
        `outputs`  Operation outputs, as specified in the toplogy template.
        """
        if isinstance(modified, Status):
            status = modified
            modified = True

        kw = dict(result=result, success=success, outputs=outputs)
        if captureException is not None:
            kw["exception"] = UnfurlTaskError(self, captureException, True)

        if success:
            return ConfiguratorResult(True, modified, status, **kw)
        elif modified:
            if not status:
                status = Status.error if self.required else Status.degraded
            return ConfiguratorResult(True, True, status, **kw)
        else:
            if status != Status.notapplied:
                status = None
            return ConfiguratorResult(False, False, None, **kw)

    # updates can be marked as dependencies (changes to dependencies changed) or required (error if changed)
    # configuration has cumulative set of changes made it to resources
    # updates update those changes
    # other configurations maybe modify those changes, triggering a configuration change
    def query(
        self,
        query,
        dependency=False,
        name=None,
        required=False,
        wantList=False,
        resolveExternal=True,
        strict=True,
    ):
        # XXX refcontext should include TARGET HOST etc
        # XXX pass resolveExternal to context?
        try:
            result = Ref(query).resolve(self.inputs.context, wantList, strict)
        except:
            UnfurlTaskError(self, "error evaluating query", True)
            return None

        if dependency:
            self.addDependency(
                query, result, name=name, required=required, wantList=wantList
            )
        return result

    def addDependency(
        self,
        expr,
        expected=None,
        schema=None,
        name=None,
        required=False,
        wantList=False,
    ):
        getter = getattr(expr, "asRef", None)
        if getter:
            # expr is a configuration or resource or ExternalValue
            expr = Ref(getter()).source

        dependency = Dependency(expr, expected, schema, name, required, wantList)
        self.dependencies[name or expr] = dependency
        self.dependenciesChanged = True
        return dependency

    def removeDependency(self, name):
        old = self.dependencies.pop(name, None)
        if old:
            self.dependenciesChanged = True
        return old

    # def createConfigurationSpec(self, name, configSpec):
    #     if isinstance(configSpec, six.string_types):
    #         configSpec = yaml.load(configSpec)
    #     return self._manifest.loadConfigSpec(name, configSpec)

    def _findConfigSpec(self, configSpecName):
        if self.configSpec.installer:
            # XXX need a way to pass different inputs
            inputs = self.configSpec.inputs
            return getConfigSpecFromInstaller(
                self.configSpec.installer, configSpecName, inputs, useDefault=False
            )
        return None

    def createSubTask(self, configSpec, resource=None, persist=False, required=False):
        from .job import TaskRequest

        if isinstance(configSpec, six.string_types):
            configSpec = self._findConfigSpec(configSpec)
            if not configSpec:
                return None

        # XXX:
        # if persist or required:
        #  expr = "::%s::.configurations::%s" % (configSpec.target, configSpec.name)
        #  self.addDependency(expr, required=required)

        if resource is None:
            resource = self.target
        return TaskRequest(configSpec, resource, persist, required)

    # # XXX how???
    # # Configurations created by subtasks are transient insofar as the are not part of the spec,
    # # but they are recorded as part of the resource's configuration state.
    # # Marking as persistent or required will create a dependency on the new configuration.
    # # XXX3 have a way to update spec attributes to trigger config updates e.g. add dns entries via attributes on a dns
    # def createSubTask(self, configSpec, persist=False, required=False):
    #   if persist or required:
    #     expr = "::%s::.configurations::%s" % (configSpec.target, configSpec.name)
    #     self.addDependency(expr, required=required)
    #   return TaskRequest(configSpec, persist, required)
    #
    # # XXX how can we explicitly associate relations with target resources etc.?
    # # through capability attributes and dependencies/relationship attributes
    def updateResources(self, resources):
        """
    Either a list or string that is parsed as YAML
    Operational state indicates if it current exists or not
    Will instantiate a new job, yield the return value to run that job right away

    .. code-block:: YAML

      - name:
        template: # name of node template
        priority: required
        dependent: boolean
        parent:
        attributes:
        status:
          readyState: ok
    """
        # XXX if template isn't specified deduce from provides and template keys
        from .manifest import Manifest
        from .job import JobRequest

        if isinstance(resources, six.string_types):
            try:
                resources = yaml.load(resources)
            except:
                UnfurlTaskError(self, "unable to parse as YAML: %s" % resources, True)
                return None

        errors = []
        newResources = []
        newResourceSpecs = []
        for resourceSpec in resources:
            originalResourceSpec = resourceSpec
            try:
                rname = resourceSpec["name"]
                if rname == ".self":
                    existingResource = self.target
                else:
                    existingResource = self.findResource(rname)
                if existingResource:
                    # XXX2 if spec is defined (not just status), there should be a way to
                    # indicate this should replace an existing resource or throw an error
                    status = resourceSpec.get("status")
                    operational = Manifest.loadStatus(status)
                    if operational.localStatus:
                        existingResource.localStatus = operational.localStatus
                    attributes = resourceSpec.get("attributes")
                    if attributes:
                        for key, value in mapValue(
                            attributes, existingResource
                        ).items():
                            existingResource.attributes[key] = value
                            logger.debug(
                                "setting attribute %s with %s on %s",
                                key,
                                value,
                                existingResource.name,
                            )
                    logger.info("updating resources %s", existingResource.name)
                    continue

                pname = resourceSpec.get('parent')
                if pname in ['.self', 'SELF']:
                  resourceSpec['parent'] = self.target.name
                elif pname == 'HOST':
                  resourceSpec['parent'] = self.target.parent.name if self.target.parent else 'root'

                resource = self._manifest.loadResource(
                    rname, resourceSpec, parent=self.target.root
                )

                # XXX wrong... these need to be operational instances
                #if resource.required or resourceSpec.get("dependent"):
                #    self.addDependency(resource, required=resource.required)
            except:
                errors.append(
                    UnfurlAddingResourceError(self, originalResourceSpec, True)
                )
            else:
                newResourceSpecs.append(originalResourceSpec)
                newResources.append(resource)

        if newResourceSpecs:
            self._resourceChanges.addResources(newResourceSpecs)
            self._addedResources.extend(newResources)
            logger.info("add resources %s", newResources)

            jobRequest = JobRequest(newResources, errors)
            if self.job:
                self.job.jobRequestQueue.append(jobRequest)
            return jobRequest
        return None


class Dependency(ChangeAware):
    """
  Represents a runtime dependency for a configuration.

  Dependencies are used to determine if a configuration needs re-run as follows:

  * They are dynamically created when evaluating and comparing the configuration spec's attributes with the previous
    values

  * Persistent dependencies can be created when the configurator invoke these apis: `createConfiguration`, `addResources`, `query`, `addDependency`
  """

    def __init__(
        self,
        expr,
        expected=None,
        schema=None,
        name=None,
        required=False,
        wantList=False,
    ):
        """
    if schema is not None, validate the result using schema
    if expected is not None, test that result equals expected
    otherwise test that result isn't empty has not changed since the last attempt
    """
        assert not (expected and schema)
        self.expr = expr

        self.expected = expected
        self.schema = schema
        self.required = required
        self.name = name
        self.wantList = wantList

    def refresh(self, config):
        if self.expected is not None:
            changeId = config.changeId
            context = RefContext(
                config.target, dict(val=self.expected, changeId=changeId)
            )
            result = Ref(self.expr).resolve(context, wantList=self.wantList)
            self.expected = result

    @staticmethod
    def hasValueChanged(value, changeset):
        if isinstance(value, Results):
            return Dependency.hasValueChanged(value._attributes, changeset)
        elif isinstance(value, collections.Mapping):
            if any(Dependency.hasValueChanged(v, changeset) for v in value.values()):
                return True
        elif isinstance(value, (collections.MutableSequence, tuple)):
            if any(Dependency.hasValueChanged(v, changeset) for v in value):
                return True
        elif isinstance(value, ChangeAware):
            return value.hasChanged(changeset)
        else:
            return False

    def hasChanged(self, config):
        changeId = config.changeId
        context = RefContext(config.target, dict(val=self.expected, changeId=changeId))
        result = Ref(self.expr).resolveOne(context)  # resolve(context, self.wantList)

        if self.schema:
            # result isn't as expected, something changed
            if not validateSchema(result, self.schema):
                return False
        else:
            if self.expected is not None:
                expected = mapValue(self.expected, context)
                if result != expected:
                    logger.debug("hasChanged: %s != %s", result, expected)
                    return True
            elif not result:
                # if expression no longer true (e.g. a resource wasn't found), then treat dependency as changed
                return True

        if self.hasValueChanged(result, config):
            return True

        return False


def getConfigSpecArgsFromImplementation(implementation, inputs=None):
    kw = dict(inputs=inputs)
    configSpecArgs = ConfigurationSpec.getDefaults()
    if isinstance(implementation, dict):
        for name, value in implementation.items():
            if name == "primary":
                implementation = value
                if isinstance(implementation, dict):
                    # it's an artifact definition
                    # XXX retrieve from repository if defined
                    implementation = implementation.get("file")
            elif name in configSpecArgs:
                kw[name] = value

    if "className" not in kw:
        try:
            lookupClass(implementation)
            kw["className"] = implementation
        except UnfurlError:
            # assume its a command line, create a ShellConfigurator
            kw["className"] = "unfurl.configurators.shell.ShellConfigurator"
            shell = inputs and inputs.get("shell")
            if shell is False or re.match(r"[\w.-]+\Z", implementation):
                # don't use the shell
                shellArgs = dict(command=[implementation])
            else:
                shellArgs = dict(command=implementation)
            if inputs:
                shellArgs.update(inputs)
            kw["inputs"] = shellArgs

    return kw


def getConfigSpecFromInstaller(configuratorTemplate, action, inputs, useDefault=True):
    operations = configuratorTemplate.properties["operations"]
    attributes = None
    if action in operations:
        # if key exist but value is None, operation explicitly not supported
        attributes = operations[action]
        if not attributes:
            return None
    elif useDefault:
        attributes = operations.get("default")
    if not attributes:
        return None

    # allow keys to be aliased:
    for i in range(len(operations)):  # avoid looping endlessly
        if not isinstance(attributes, six.string_types):
            break
        attributes = operations.get(attributes)

    if not isinstance(attributes, dict):
        return None

    # merge in defaults
    defaults = operations.get("shared")
    if defaults:
        attributes = dict(defaults, **attributes)
    if "implementation" not in attributes:
        return None

    installerInputs = attributes.get("inputs", {})
    if inputs:
        installerInputs = dict(installerInputs, **inputs)

    kw = getConfigSpecArgsFromImplementation(
        attributes["implementation"], installerInputs
    )
    kw["installer"] = configuratorTemplate
    return ConfigurationSpec(configuratorTemplate.name, action, **kw)
