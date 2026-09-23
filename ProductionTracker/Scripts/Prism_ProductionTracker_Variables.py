# -*- coding: utf-8 -*-
#
# Production Tracker - a Prism Pipeline plugin
# Copyright (C) 2026 Trond Hille
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option)
# any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License in the
# LICENSE file, or <https://www.gnu.org/licenses/>, for more details.
#
# Commercial licences, which lift the GPL obligations for proprietary use,
# are available separately from the copyright holder.
#
####################################################
#
# Production Tracker - a custom Prism plugin
#
####################################################


import os


# Single source of truth - the Help -> About box reads these too, so the
# version is only ever written down once.
PLUGIN_NAME = "Production Tracker"
PLUGIN_VERSION = "v1.0.1"
PLUGIN_URL = "https://github.com/Tronotrond/Prism_ProjectTracker"


class Prism_ProductionTracker_Variables(object):
    def __init__(self, core, plugin):
        self.version = PLUGIN_VERSION
        self.pluginName = "ProductionTracker"
        self.pluginType = "Custom"
        self.platforms = ["Windows", "Linux", "Darwin"]
        self.pluginDirectory = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
