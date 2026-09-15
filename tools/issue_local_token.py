"""Issue a short-lived local token for development and API smoke tests."""
import argparse, os
from planpilot.security import issue_token

parser=argparse.ArgumentParser(); parser.add_argument('--user',required=True); parser.add_argument('--role',choices=('planner','manager'),required=True); parser.add_argument('--ttl',type=int,default=3600); args=parser.parse_args()
secret=os.environ.get('PLANPILOT_AUTH_SECRET')
if not secret: raise SystemExit('PLANPILOT_AUTH_SECRET is required')
print(issue_token(args.user,args.role,secret,args.ttl))
