"""Configration exceptions"""

class InvalidConfigFile(BaseException):
    """Invalid configuration file"""


class InvalidConfigImplementation(BaseException):
    """Invalid usage of annotations for configuration"""
    # you shouldn't catch this exception
    # it indicates invalid code, not invalid configuration file itself
