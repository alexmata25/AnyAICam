import json
import sys

from cloud_features import deployment_status


def main():
    result=deployment_status()
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='ready' else 1


if __name__=='__main__': sys.exit(main())
