#### load env from .env file using load_dotenv
import os
from dotenv import load_dotenv

load_dotenv()

class config:
    def __init__(self):
        self.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
        self.server_config = {
        "server":{
            "hostname": "54.89.249.254",
            "username": "ec2-user",
            "password": "",
            "key_filename": "abc.pem"
        } }

setting = config()