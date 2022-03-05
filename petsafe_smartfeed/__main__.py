import sys
import argparse

from petsafe_smartfeed import PetSafeClient

# create parser for arguments
parser = argparse.ArgumentParser(
    usage="python -m petsafe_smartfeed email [-t email_code]"
)
parser.add_argument("email", help="account email address")

# if no arguments specified, show help
if len(sys.argv) < 2:
    parser.print_help()
    sys.exit(1)

# parse for arguments
args = parser.parse_args()

client = PetSafeClient(email=args.email)
client.request_code()
print("Code requested, please check your email.")
print("")

code = input("Enter email code: ")
client.request_tokens_from_code(code)

print("")
print("IdToken:", client.id_token)
print("AccessToken", client.access_token)
print("RefreshToken", client.refresh_token)
