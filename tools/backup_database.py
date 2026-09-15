import argparse
from planpilot.backup import backup_database
parser=argparse.ArgumentParser(); parser.add_argument('source'); parser.add_argument('destination'); args=parser.parse_args(); backup_database(args.source,args.destination)
