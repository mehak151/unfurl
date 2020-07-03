"""
A Job is generated by comparing a list of specs with the last known state of the system.
Job runs tasks, each of which has a configuration spec that is executed on the running system
Each task tracks and records its modifications to the system's state
"""

import collections
import datetime
import types
import itertools
import os
from .support import Status, Priority, Defaults, AttributeManager
from .result import serializeValue, ChangeRecord
from .util import UnfurlError, UnfurlTaskError, toEnum
from .merge import mergeDicts
from .runtime import OperationalInstance
from .configurator import TaskView, ConfiguratorResult, TaskRequest, JobRequest
from .plan import Plan, DeployPlan
from . import display

import logging

logger = logging.getLogger("unfurl")


class ConfigChange(OperationalInstance, ChangeRecord):
    """
  Represents a configuration change made to the system.
  It has a operating status and a list of dependencies that contribute to its status.
  There are two kinds of dependencies:

  1. Live resource attributes that the configuration's inputs depend on.
  2. Other configurations and resources it relies on to function properly.
  """

    def __init__(self, status=None, **kw):
        OperationalInstance.__init__(self, status, **kw)
        ChangeRecord.__init__(self)


class JobOptions(object):
    """
  Options available to select which tasks are run, e.g. read-only

  does the config apply to the action?
  is it out of date?
  is it in a ok state?
  """

    defaults = dict(
        parentJob=None,
        startTime=None,
        out=None,
        verbose=0,
        instance=None,
        instances=None,
        template=None,
        useConfigurator=False,
        # default options:
        add=True,  # add new templates
        update=True,  # run configurations that whose spec has changed but don't require a major version change
        repair="error",  # or 'degraded' or "missing" or "none", run configurations that are not operational and/or degraded
        upgrade=False,  # run configurations with major version changes or whose spec has changed
        all=False,  # (re)run all configurations
        verify=False,  # XXX3 discover first and set status if it differs from expected state
        readonly=False,  # only run configurations that won't alter the system
        dryrun=False,
        planOnly=False,
        requiredOnly=False,
        prune=False,
        append=None,
        replace=None,
        commit=True,
        dirty=False,  # run the job even if the repository has uncommitted changrs
        workflow=Defaults.workflow,
    )

    def __init__(self, **kw):
        options = self.defaults.copy()
        options["instance"] = kw.get("resource")  # old option name
        options.update(kw)
        self.__dict__.update(options)
        self.userConfig = kw

    def getUserSettings(self):
        # only include settings different from the defaults
        return {
            k: self.userConfig[k]
            for k in set(self.userConfig) & set(self.defaults)
            if k != "out" and self.userConfig[k] != self.defaults[k]
        }


class ConfigTask(ConfigChange, TaskView, AttributeManager):
    """
    receives a configSpec and a target node instance
    instantiates and runs Configurator
    updates Configurator's target's status and lastConfigChange
  """

    def __init__(self, job, configSpec, target, parentId=None, reason=None):
        ConfigChange.__init__(self)
        TaskView.__init__(self, job.runner.manifest, configSpec, target, reason)
        AttributeManager.__init__(self)
        self.parentId = parentId or job.changeId
        self.changeId = self.parentId
        self.startTime = job.startTime or datetime.datetime.now()
        self.dryRun = job.dryRun
        self.verbose = job.verbose
        self._configurator = None
        self.generator = None
        self.job = job
        self.changeList = []
        self.result = None
        self.outputs = None
        # self._completedSubTasks = []

        # set the attribute manager on the root resource
        # XXX refcontext in attributeManager should define $TARGET $HOST etc.
        # self.configuratorResource.root.attributeManager = self
        self.target.root.attributeManager = self

    def priority():
        doc = "The priority property."

        def fget(self):
            if self._priority is None:
                return self.configSpec.shouldRun()
            else:
                return self._priority

        def fset(self, value):
            self._priority = value

        def fdel(self):
            del self._priority

        return locals()

    priority = property(**priority())

    @property
    def configurator(self):
        if self._configurator is None:
            self._configurator = self.configSpec.create()
        return self._configurator

    def startRun(self):
        self.generator = self.configurator.getGenerator(self)
        assert isinstance(self.generator, types.GeneratorType)

    def send(self, change):
        result = None
        # if isinstance(change, ConfigTask):
        #     self._completedSubTasks.append(change)
        try:
            result = self.generator.send(change)
        finally:
            # serialize configuration changes
            self.commitChanges()
        return result

    def start(self):
        self.startRun()

    def _updateStatus(self, result):
        """
        Update the instances status with the result of the operation.
        If status wasn't explicitly set but the operation changed the instance's configuration
        or state, choose a status based on the type of operation.
        """

        if result.status is not None:
            # status was explicitly set
            self.target.localStatus = result.status
        elif not result.success:
            # if any task failed and (maybe) modified, target.status will be set to error or unknown
            if result.modified:
                self.target.localStatus = (
                    Status.error if self.required else Status.degraded
                )
            elif result.modified is None:
                self.target.localStatus = Status.unknown
            # otherwise doesn't modify target status

    def _updateLastChange(self, result):
        """
      If the target's configuration or state has changed, set the instance's lastChange
      state to this tasks' changeid.
      """
        if self.target.lastChange is None:
            # hacky but always save _lastConfigChange the first time to
            # distinguish this from a brand new resource
            self.target._lastConfigChange = self.changeId
        if result.modified or self._resourceChanges.getAttributeChanges(
            self.target.key
        ):
            self.target._lastStateChange = self.changeId

    def finished(self, result):
        assert result
        if self.generator:
            self.generator.close()
            self.generator = None

        self.outputs = result.outputs

        # don't set the changeId until we're finish so that we have a higher changeid
        # than nested tasks and jobs that ran (avoids spurious config changed tasks)
        self.changeId = self.job.runner.incrementChangeId()
        # XXX2 if attributes changed validate using attributesSchema
        # XXX2 Check that configuration provided the metadata that it declared (check postCondition)

        if self.changeList:
            # merge changes together (will be saved with changeset)
            changes = self.changeList
            accum = changes.pop(0)
            while changes:
                accum = mergeDicts(accum, changes.pop(0))

            self._resourceChanges.updateChanges(
                accum, self.statuses, self.target, self.changeId
            )
            # XXX implement:
            # if not result.applied:
            #    self._resourceChanges.rollback(self.target)

        # now that resourceChanges finalized:
        self._updateStatus(result)
        self._updateLastChange(result)
        self.result = result
        self.localStatus = Status.ok if result.success else Status.error
        return self

    def commitChanges(self):
        """
    This can be called multiple times if the configurator yields multiple times.
    Save the changes made each time.
    """
        changes = AttributeManager.commitChanges(self)
        self.changeList.append(changes)
        return changes

    def hasInputsChanged(self):
        """
    Evaluate configuration spec's inputs and compare with the current inputs' values
    """
        _parameters = None
        if self.lastConfigChange:  # XXX this isn't set right now
            changeset = self._manifest.loadConfigChange(self.lastConfigChange)
            _parameters = changeset.inputs
        if not _parameters:
            return not not self.inputs

        if set(self.inputs.keys()) != set(_parameters.keys()):
            return True  # params were added or removed

        # XXX3 not all parameters need to be live
        # add an optional liveParameters attribute to config spec to specify which ones to check

        # compare old with new
        for name, val in self.inputs.items():
            if serializeValue(val) != _parameters[name]:
                return True
            # XXX if the value changed since the last time we checked
            # if Dependency.hasValueChanged(val, lastChecked):
            #  return True
        return False

    def hasDependenciesChanged(self):
        return any(d.hasChanged(self) for d in self.dependencies.values())

    def refreshDependencies(self):
        for d in self.dependencies.values():
            d.refresh(self)

    def summary(self):
        if self.target.name != self.target.template.name:
            rname = "%s (%s)" % (self.target.name, self.target.template.name)
        else:
            rname = self.target.name

        if self.configSpec.name != self.configSpec.className:
            cname = "%s (%s)" % (self.configSpec.name, self.configSpec.className)
        else:
            cname = self.configSpec.name
        return (
            "{action} on instance {rname} (type {rtype}, status {rstatus}) "
            + "using configurator {cname}, priority: {priority}, reason: {reason}"
        ).format(
            action=self.configSpec.operation,
            rname=rname,
            rtype=self.target.template.type,
            rstatus=self.target.status.name,
            cname=cname,
            priority=self.priority.name,
            reason=self.reason or "",
        )

    def __repr__(self):
        return "ConfigTask(%s:%s %s)" % (
            self.target,
            self.configSpec.name,
            self.reason or "unknown",
        )


class Job(ConfigChange):
    """
  runs ConfigTasks and Jobs
  """

    MAX_NESTED_SUBTASKS = 100

    def __init__(self, runner, rootResource, plan, jobOptions):
        super(Job, self).__init__(Status.ok)
        assert isinstance(jobOptions, JobOptions)
        self.__dict__.update(jobOptions.__dict__)
        self.dryRun = jobOptions.dryrun
        if self.startTime is None:
            self.startTime = datetime.datetime.now()
        self.jobOptions = jobOptions
        self.runner = runner
        self.plan = plan
        self.rootResource = rootResource
        self.jobRequestQueue = []
        self.unexpectedAbort = None
        # note: tasks that never run will all share this changeid
        self.changeId = runner.incrementChangeId()
        self.parentId = self.parentJob.changeId if self.parentJob else None
        self.workDone = collections.OrderedDict()

    def createTask(self, configSpec, target, parentId=None, reason=None):
        # XXX2 if operation_host set, create remote task instead
        task = ConfigTask(self, configSpec, target, parentId, reason=reason)
        try:
            task.inputs
            task.configurator
        except Exception:
            UnfurlTaskError(task, "unable to create task", True)

        # if configSpec.hasBatchConfigurator():
        # search targets parents for a batchConfigurator
        # XXX how to associate a batchConfigurator with a resource and when is its task created?
        # batchConfigurator tasks more like a job because they have multiple changeids
        #  batchConfiguratorJob = findBatchConfigurator(configSpec, target)
        #  batchConfiguratorJob.add(task)
        #  return None

        return task

    def filterConfig(self, config, target):
        opts = self.jobOptions
        if opts.readonly and config.workflow != "discover":
            return None, "read only"
        if opts.requiredOnly and not config.required:
            return None, "required"
        if opts.instance and target.name != opts.instance:
            return None, "instance"
        if opts.instances and target.name not in opts.instances:
            return None, "instances"
        return config, None

    def getCandidateTasks(self):
        # XXX plan might call job.runJobRequest(configuratorJob) before yielding
        planGen = self.plan.executePlan()
        result = None
        try:
            while True:
                req = planGen.send(result)
                configSpec = req.configSpec
                if req.error:
                    # placeholder configspec for errors: has an error message instead of className
                    # create a task so we can record this failure like other task failures
                    errorTask = ConfigTask(
                        self, configSpec, req.target, reason=req.reason
                    )
                    # the task won't run if we associate an exception with it:
                    UnfurlTaskError(errorTask, configSpec.className, True)
                    result = yield errorTask
                    continue

                configSpecName = configSpec.name
                configSpec, filterReason = self.filterConfig(configSpec, req.target)
                if not configSpec:
                    logger.debug(
                        "skipping configspec %s for %s: doesn't match %s filter",
                        configSpecName,
                        req.target.name,
                        filterReason,
                    )
                    result = None  # treat as filtered step
                    continue

                oldResult = self.runner.isConfigAlreadyHandled(configSpec, req.target)
                if oldResult:
                    # configuration may have premptively run while executing another task
                    logger.debug(
                        "configspec %s for target %s already handled",
                        configSpecName,
                        req.target.name,
                    )
                    result = oldResult
                    continue

                result = yield self.createTask(
                    configSpec, req.target, reason=req.reason
                )
        except StopIteration:
            pass

    def validateJobOptions(self):
        if self.jobOptions.instance and not self.rootResource.findResource(
            self.jobOptions.instance
        ):
            logger.warning(
                'selected instance not found: "%s"', self.jobOptions.instance
            )

    def run(self):
        self.validateJobOptions()
        taskGen = self.getCandidateTasks()
        result = None
        try:
            while True:
                task = taskGen.send(result)
                self.runner.addWork(task)
                if not self.shouldRunTask(task):
                    result = None  # treat as filtered step
                    continue

                if self.jobOptions.planOnly:
                    if not self.cantRunTask(task):
                        # pretend run was sucessful
                        logger.info("Run " + task.summary())
                        result = task.finished(
                            ConfiguratorResult(True, True, Status.ok)
                        )
                    else:
                        result = task.finished(ConfiguratorResult(False, False))
                else:
                    logger.info("Running task %s", task)
                    result = self.runTask(task)

                if self.shouldAbort(task):
                    return self.rootResource
        except StopIteration:
            pass

        # the only jobs left will be those that were added to resources already iterated over
        # and were not yielding inside runTask
        while self.jobRequestQueue:
            jobRequest = self.jobRequestQueue[0]
            job = self.runJobRequest(jobRequest)
            if self.shouldAbort(job):
                return self.rootResource

        # XXX
        # if not self.parentJob:
        #   # create a job that will re-run configurations whose parameters or runtime dependencies have changed
        #   # ("config changed" tasks)
        #   # XXX3 check for orphaned resources and mark them as orphaned
        #   #  (a resource is orphaned if it was added as a dependency and no longer has dependencies)
        #   #  (orphaned resources can be deleted by the configuration that created them or manages that type)
        #   maxloops = 10 # XXX3 better loop detection
        #   for count in range(maxloops):
        #     jobOptions = JobOptions(parentJob=self, repair='none')
        #     plan = Plan(self.rootResource, self.runner.manifest.tosca, jobOptions)
        #     job = Job(self.runner, self.rootResource, plan, jobOptions)
        #     job.run()
        #     # break when there are no more tasks to run
        #     if not len(job.workDone) or self.shouldAbort(job):
        #       break
        #   else:
        #     raise UnfurlError("too many final dependency runs")

        return self.rootResource

    def runJobRequest(self, jobRequest):
        logger.debug("running jobrequest: %s", jobRequest)
        self.jobRequestQueue.remove(jobRequest)
        resourceNames = [r.name for r in jobRequest.instances]
        jobOptions = JobOptions(
            parentJob=self, repair="none", all=True, instances=resourceNames
        )
        childJob = self.runner.createJob(jobOptions)
        assert childJob.parentJob is self
        childJob.run()
        return childJob

    def shouldRunTask(self, task):
        """
    Checked at runtime right before each task is run
    """
        try:
            if task._configurator:
                priority = task.configurator.shouldRun(task)
            else:
                priority = task.priority
        except Exception:
            # unexpected error don't run this
            UnfurlTaskError(task, "shouldRun failed unexpectedly", True)
            return False

        if isinstance(priority, bool):
            priority = priority and Priority.required or Priority.ignore
        else:
            priority = toEnum(Priority, priority)
        if priority != task.priority:
            logger.debug(
                "configurator changed task %s priority from %s to %s",
                task,
                task.priority,
                priority,
            )
            task.priority = priority
        return priority > Priority.ignore

    def cantRunTask(self, task):
        """
    Checked at runtime right before each task is run

    * validate inputs
    * check pre-conditions to see if it can be run
    * check task if it can be run
    """
        canRun = False
        reason = ""
        try:
            if task.errors:
                canRun = False
                reason = "could not create task"
                return
            if task.dryRun and not task.configurator.canDryRun(task):
                canRun = False
                reason = "dry run not supported"
                return
            missing = []
            skipDependencyCheck = False
            if not skipDependencyCheck:
                dependencies = list(task.target.getOperationalDependencies())
                missing = [
                    dep for dep in dependencies if not dep.operational and dep.required
                ]
            if missing:
                reason = "missing required dependencies: %s" % ",".join(
                    [dep.name for dep in missing]
                )
            else:
                errors = task.configSpec.findInvalidateInputs(task.inputs)
                if errors:
                    reason = "invalid inputs: %s" % str(errors)
                else:
                    preErrors = task.configSpec.findInvalidPreconditions(task.target)
                    if preErrors:
                        reason = "invalid preconditions: %s" % str(preErrors)
                    else:
                        errors = task.configurator.canRun(task)
                        if not errors or not isinstance(errors, bool):
                            reason = "configurator declined: %s" % str(errors)
                        else:
                            canRun = True
        except Exception:
            UnfurlTaskError(task, "cantRunTask failed unexpectedly", True)
            reason = "unexpected exception in cantRunTask"
            canRun = False
        finally:
            if canRun:
                return False
            else:
                logger.info("could not run task %s: %s", task, reason)
                return "could not run: " + reason

    def shouldAbort(self, task):
        return False  # XXX3

    def jsonSummary(self):
        return dict(
            outputs=serializeValue(self.getOutputs()),
            job=dict(
                id=self.changeId, status=self.status.name, tasks=len(self.workDone)
            ),
            tasks=[[name, task.status.name] for (name, task) in self.workDone.items()],
        )

    def stats(self, asMessage=False):
        tasks = self.workDone.values()
        key = lambda t: t._localStatus or Status.unknown
        tasks = sorted(tasks, key=key)
        stats = dict(total=len(tasks), ok=0, error=0, unknown=0, skipped=0)
        for k, g in itertools.groupby(tasks, key):
            if not k:
                stats["skipped"] = len(list(g))
            else:
                stats[k.name] = len(list(g))
        stats["changed"] = len([t for t in tasks if t.result and t.result.modified])
        if asMessage:
            return "{total} tasks ({changed} changed, {ok} ok, {error} failed, {unknown} unknown, {skipped} skipped)".format(
                **stats
            )
        return stats

    def summary(self):
        outputString = ""
        outputs = self.getOutputs()
        if outputs:
            outputString = "\nOutputs:\n    " + "\n    ".join(
                "%s: %s" % (name, value)
                for name, value in serializeValue(outputs).items()
            )

        if not self.workDone:
            return "Job %s completed: %s. Found nothing to do. %s" % (
                self.changeId,
                self.status.name,
                outputString,
            )

        def format(i, name, task):
            return "%d. %s; %s" % (i, task.summary(), task.result or "skipped")

        line1 = "Job %s completed: %s. %s:\n    " % (
            self.changeId,
            self.status.name,
            self.stats(asMessage=True),
        )
        tasks = "\n    ".join(
            format(i + 1, name, task)
            for i, (name, task) in enumerate(self.workDone.items())
        )
        return line1 + tasks + outputString

    def getOperationalDependencies(self):
        # XXX3 this isn't right, root job might have too many and child job might not have enough
        # plus dynamic configurations probably shouldn't be included if yielded by a configurator
        for task in self.workDone.values():
            yield task

    def getOutputs(self):
        return self.rootResource.outputs.attributes

    def runQuery(self, query, trace=0):
        from .eval import evalForFunc, RefContext

        return evalForFunc(query, RefContext(self.rootResource, trace=trace))

    def runTask(self, task, depth=0):
        """
    During each task run:
    * Notification of metadata changes that reflect changes made to resources
    * Notification of add or removing dependency on a resource or properties of a resource
    * Notification of creation or deletion of a resource
    * Requests a resource with requested metadata, if it doesn't exist, a task is run to make it so
    (e.g. add a dns entry, install a package).
    """
        errors = self.cantRunTask(task)
        if errors:
            return task.finished(ConfiguratorResult(False, False, result=errors))

        task.start()
        change = None
        while True:
            try:
                result = task.send(change)
            except Exception:
                UnfurlTaskError(task, "configurator.run failed", True)
                return task.finished(ConfiguratorResult(False, None, Status.error))
            if isinstance(result, TaskRequest):
                if depth >= self.MAX_NESTED_SUBTASKS:
                    UnfurlTaskError(task, "too many subtasks spawned", True)
                    change = task.finished(ConfiguratorResult(False, None))
                else:
                    subtask = self.createTask(
                        result.configSpec, result.target, self.changeId
                    )
                    self.runner.addWork(subtask)
                    # returns the subtask with result
                    change = self.runTask(subtask, depth + 1)
            elif isinstance(result, JobRequest):
                job = self.runJobRequest(result)
                change = job
            elif isinstance(result, ConfiguratorResult):
                retVal = task.finished(result)
                logger.info(
                    "finished running task %s: %s; %s", task, task.target.status, result
                )
                return retVal
            else:
                UnfurlTaskError(task, "unexpected result from configurator", True)
                return task.finished(ConfiguratorResult(False, None, Status.error))


class Runner(object):
    def __init__(self, manifest):
        self.manifest = manifest
        assert self.manifest.tosca
        self.lastChangeId = manifest.lastChangeId
        self.currentJob = None

    def addWork(self, task):
        key = "%s:%s:%s:%s" % (
            task.target.name,
            task.configSpec.name,
            task.configSpec.operation,
            task.changeId,
        )
        self.currentJob.workDone[key] = task
        task.job.workDone[key] = task

    def isConfigAlreadyHandled(self, configSpec, target):
        return None  # XXX
        # return configSpec.name in self.currentJob.workDone

    def createJob(self, joboptions):
        """
    Selects task to run based on job options and starting state of manifest
    """
        root = self.manifest.getRootResource()
        assert self.manifest.tosca
        WorkflowPlan = Plan.getPlanClassForWorkflow(joboptions.workflow)
        if not WorkflowPlan:
            raise UnfurlError("unknown workflow: %s" % joboptions.workflow)
        plan = WorkflowPlan(root, self.manifest.tosca, joboptions)
        return Job(self, root, plan, joboptions)

    def incrementChangeId(self):
        self.lastChangeId += 1
        return self.lastChangeId

    def run(self, jobOptions=None):
        """
    """
        try:
            cwd = os.getcwd()
            if self.manifest.getBaseDir():
                os.chdir(self.manifest.getBaseDir())
            if jobOptions is None:
                jobOptions = JobOptions()
            if jobOptions.commit and not jobOptions.dirty and self.manifest.localEnv:
                for repo in self.manifest.localEnv.getRepos():
                    if repo.isDirty():
                        logger.error(
                            "aborting run: uncommitted files (--dirty to override)"
                        )
                        return None
            job = self.createJob(jobOptions)
            self.currentJob = job
            try:
                display.verbosity = jobOptions.verbose
                job.run()
            except Exception:
                job.localStatus = Status.error
                job.unexpectedAbort = UnfurlError(
                    "unexpected exception while running job", True, True
                )
            self.currentJob = None
            self.manifest.commitJob(job)
        finally:
            os.chdir(cwd)
        return job
