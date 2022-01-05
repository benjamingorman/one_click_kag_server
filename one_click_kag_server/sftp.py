import paramiko
import logging
import os
from stat import S_ISDIR

class MySFTPClient(paramiko.SFTPClient):
    def put_dir(self, source, target):
        ''' Uploads the contents of the source directory to the target path. The
            target directory needs to exists. All subdirectories in source are 
            created under target.
        '''
        for item in os.listdir(source):
            if os.path.isfile(os.path.join(source, item)):
                logging.info("Uploading %s", item)
                self.put(os.path.join(source, item), '%s/%s' % (target, item))
            else:
                self.mkdir('%s/%s' % (target, item), ignore_existing=True)
                self.put_dir(os.path.join(source, item), '%s/%s' % (target, item))

    def mkdir(self, path, mode=511, ignore_existing=False):
        ''' Augments mkdir by adding an option to not fail if the folder exists  '''
        try:
            super(MySFTPClient, self).mkdir(path, mode)
        except IOError:
            if ignore_existing:
                pass
            else:
                raise
    def get_recursive(self, path, dest):
        ''' Download folder recursively '''
        item_list = self.listdir_attr(path)
        dest = str(dest)
        if not os.path.isdir(dest):
            os.makedirs(dest, exist_ok=True)
        for item in item_list:
            mode = item.st_mode
            if S_ISDIR(mode):
                self.get_recursive(path + "/" + item.filename, dest + "/" + item.filename)
            else:
                self.get(path + "/" + item.filename, dest + "/" + item.filename)