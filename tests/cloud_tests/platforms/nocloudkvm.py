# This file is part of cloud-init. See LICENSE file for license information.

"""Base NoCloud KVM platform."""
import glob
import os

from simplestreams import filters
from simplestreams import mirrors
from simplestreams import objectstores
from simplestreams import util as s_util

from cloudinit import util as c_util
from tests.cloud_tests.images import nocloudkvm as nocloud_kvm_image
from tests.cloud_tests.instances import nocloudkvm as nocloud_kvm_instance
from tests.cloud_tests.platforms import base
from tests.cloud_tests import util


class NoCloudKVMPlatform(base.Platform):
    """NoCloud KVM test platform."""

    platform_name = 'nocloud-kvm'

    def get_image(self, img_conf):
        """Get image using specified image configuration.

        @param img_conf: configuration for image
        @return_value: cloud_tests.images instance
        """
        (url, path) = s_util.path_from_mirror_url(img_conf['mirror_url'], None)

        filter = filters.get_filters(['arch=%s' % c_util.get_architecture(),
                                      'release=%s' % img_conf['release'],
                                      'ftype=disk1.img'])
        mirror_config = {'filters': filter,
                         'keep_items': False,
                         'max_items': 1,
                         'checksumming_reader': True,
                         'item_download': True
                         }

        def policy(content, path):
            return s_util.read_signed(content, keyring=img_conf['keyring'])

        smirror = mirrors.UrlMirrorReader(url, policy=policy)
        tstore = objectstores.FileStore(img_conf['mirror_dir'])
        tmirror = mirrors.ObjectFilterMirror(config=mirror_config,
                                             objectstore=tstore)
        tmirror.sync(smirror, path)

        search_d = os.path.join(img_conf['mirror_dir'], '**',
                                img_conf['release'], '**', '*.img')

        images = []
        for fname in glob.iglob(search_d, recursive=True):
            images.append(fname)

        if len(images) != 1:
            raise Exception('No unique images found')

        image = nocloud_kvm_image.NoCloudKVMImage(self, img_conf, images[0])
        if img_conf.get('override_templates', False):
            image.update_templates(self.config.get('template_overrides', {}),
                                   self.config.get('template_files', {}))
        return image

    def create_image(self, properties, config, features,
                     src_img_path, image_desc=None, use_desc=None,
                     user_data=None, meta_data=None):
        """Create an image

        @param src_img_path: image path to launch from
        @param properties: image properties
        @param config: image configuration
        @param features: image features
        @param image_desc: description of image being launched
        @param use_desc: description of container's use
        @return_value: cloud_tests.instances instance
        """
        name = util.gen_instance_name(image_desc=image_desc, use_desc=use_desc)
        img_path = os.path.join(self.config['data_dir'], name + '.qcow2')
        c_util.subp(['qemu-img', 'create', '-f', 'qcow2',
                    '-b', src_img_path, img_path])

        return nocloud_kvm_instance.NoCloudKVMInstance(self, img_path,
                                                       properties, config,
                                                       features, user_data,
                                                       meta_data)

# vi: ts=4 expandtab
