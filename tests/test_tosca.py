import unittest
from giterop.yamlmanifest import YamlManifest
from giterop.job import Runner, JobOptions
from giterop.support import Status
from giterop.configurator import Configurator
# from giterop.util import GitErOpError, GitErOpValidationError

import logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger('giterup')
logger.setLevel(logging.DEBUG)

class SetAttributeConfigurator(Configurator):
  def run(self, task):
    task.target.attributes['private_address'] = '10.0.0.1'
    yield task.createResult(True, True, Status.ok)

manifestDoc = '''
apiVersion: giterops/v1alpha1
kind: Manifest
spec:
  inputs:
    cpus: 2
  tosca:
    tosca_definitions_version: tosca_simple_yaml_1_0
    topology_template:
      inputs:
        cpus:
          type: integer
          description: Number of CPUs for the server.
          constraints:
            - valid_values: [ 1, 2, 4, 8 ]
      outputs:
        server_ip:
          description: The private IP address of the provisioned server.
          # equivalent to { get_attribute: [ my_server, private_address ] }
          value: {eval: "::my_server::private_address"}
      node_templates:
        my_server:
          type: tosca.nodes.Compute
          capabilities:
            # Host container properties
            host:
             properties:
               num_cpus: { get_input: cpus }  # {eval: "::root::inputs::cpus"} # { get_input: cpus }
               disk_size: 10 GB
               mem_size: 512 MB
            # Guest Operating System properties
            os:
              properties:
                # host Operating System image properties
                architecture: x86_64
                type: Linux
                distribution: RHEL
                version: 6.5
          interfaces:
           Standard:
            create:
              inputs:
                parameters:
                priority:
                parameterSchema:
              implementation:
                primary: SetAttributeConfigurator
                timeout: 120
'''

class ToscaSyntaxTest(unittest.TestCase):
  def test_inputAndOutputs(self):
    manifest = YamlManifest(manifestDoc)
    job = Runner(manifest).run(JobOptions(add=True, startTime="time-to-test"))
    assert not job.unexpectedAbort, job.unexpectedAbort.getStackTrace()
    assert job.getOutputs()['server_ip'], '10.0.0.1'
    assert job.status == Status.ok, job.summary()
