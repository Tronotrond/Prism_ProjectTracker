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


from Prism_ProductionTracker_Variables import Prism_ProductionTracker_Variables
from Prism_ProductionTracker_Functions import Prism_ProductionTracker_Functions


class Prism_ProductionTracker(Prism_ProductionTracker_Variables, Prism_ProductionTracker_Functions):
    def __init__(self, core):
        Prism_ProductionTracker_Variables.__init__(self, core, self)
        Prism_ProductionTracker_Functions.__init__(self, core, self)
