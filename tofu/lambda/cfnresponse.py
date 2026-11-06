"""Stand-in for the `cfnresponse` module that CloudFormation injects into inline
(ZipFile) Lambda functions.

backfill.py imports cfnresponse at module load. Outside CloudFormation the module
does not exist, so this shim keeps the import working. The OpenTofu module invokes
the function directly (no 'ResponseURL' in the event), which never reaches
cfnresponse.send().
"""

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(event, context, response_status, response_data=None, *args, **kwargs):
    raise RuntimeError("cfnresponse.send is only available when invoked by CloudFormation")
