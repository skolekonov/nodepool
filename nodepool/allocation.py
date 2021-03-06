#!/usr/bin/env python

# Copyright (C) 2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
#
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This module holds classes that represent concepts in nodepool's
allocation algorithm.

The algorithm is:

  Setup:

  * Establish the node providers with their current available
    capacity.
  * Establish requests that are to be made of each provider for a
    certain image.
  * Indicate which providers can supply nodes of that image.
  * Indicate to which targets nodes of a certain image from a certain
    provider may be distributed (and the weight that should be
    given to each target when distributing).

  Run:

  * For each image, set the requested number of nodes from each
    provider to be proportional to that providers overall capacity.

  * Define the 'priority' of a request as the number of requests for
    the same image from other providers.

  * For each provider, sort the requests by the priority.  This puts
    requests that can be serviced by the fewest providers first.

  * Grant each such request in proportion to that requests portion of
    the total amount requested by requests of the same priority.

  * The nodes allocated by a grant are then distributed to the targets
    which are associated with the provider and image, in proportion to
    that target's portion of the sum of the weights of each target for
    that image.
"""


class AllocationProvider(object):
    """A node provider and its capacity."""
    def __init__(self, name, available):
        self.name = name
        self.available = available
        self.sub_requests = []
        self.grants = []

    def __repr__(self):
        return '<AllocationProvider %s>' % self.name

    def makeGrants(self):
        reqs = self.sub_requests[:]
        # Sort the requests by priority so we fill the most specific
        # requests first (e.g., if this provider is the only one that
        # can supply foo nodes, then it should focus on supplying them
        # and leave bar nodes to other providers).
        reqs.sort(lambda a, b: cmp(a.getPriority(), b.getPriority()))
        for req in reqs:
            total_requested = 0.0
            # Within a specific priority, grant requests
            # proportionally.
            reqs_at_this_level = [r for r in reqs
                                  if r.getPriority() == req.getPriority()]
            for r in reqs_at_this_level:
                total_requested += r.amount
            if total_requested:
                ratio = float(req.amount) / total_requested
            else:
                ratio = 0.0
            grant = int(round(req.amount * ratio))
            grant = min(grant, self.available)
            # This adjusts our availability as well as the values of
            # other requests, so values will be correct the next time
            # through the loop.
            req.grant(grant)


class AllocationRequest(object):
    """A request for a number of images."""
    def __init__(self, name, amount):
        self.name = name
        self.amount = float(amount)
        # Sub-requests of individual providers that make up this
        # request.  AllocationProvider -> AllocationSubRequest
        self.sub_requests = {}
        # Targets to which nodes from this request may be assigned.
        # Mostly here to hold the min_ready for the image for this
        # target.  AllocationTarget -> AllocationRequestTarget
        self.request_targets = {}

    def __repr__(self):
        return '<AllocationRequest for %s of %s>' % (self.amount, self.name)

    def addTarget(self, target, min_ready, current):
        art = AllocationRequestTarget(self, target, min_ready, current)
        self.request_targets[target] = art

    def addProvider(self, provider, target):
        # Handle being called multiple times with different targets.
        s = self.sub_requests.get(provider)
        if not s:
            s = AllocationSubRequest(self, provider)
        agt = s.addTarget(self.request_targets[target])
        self.sub_requests[provider] = s
        if s not in provider.sub_requests:
            provider.sub_requests.append(s)
        self.makeRequests()
        return s, agt

    def makeRequests(self):
        # (Re-)distribute this request across all of its providers.
        total_available = 0.0
        for sub_request in self.sub_requests.values():
            total_available += sub_request.provider.available
        for sub_request in self.sub_requests.values():
            if total_available:
                ratio = float(sub_request.provider.available) / total_available
            else:
                ratio = 0.0
            sub_request.setAmount(ratio * self.amount)


class AllocationSubRequest(object):
    """A request for a number of images from a specific provider."""
    def __init__(self, request, provider):
        self.request = request
        self.provider = provider
        self.amount = 0.0
        self.targets = []

    def __repr__(self):
        return '<AllocationSubRequest for %s (out of %s) of %s from %s>' % (
            self.amount, self.request.amount, self.request.name,
            self.provider.name)

    def addTarget(self, request_target):
        agt = AllocationGrantTarget(self, request_target)
        self.targets.append(agt)
        return agt

    def setAmount(self, amount):
        self.amount = amount

    def getPriority(self):
        return len(self.request.sub_requests)

    def grant(self, amount):
        # Grant this request (with the supplied amount).  Adjust this
        # sub-request's value to the actual, as well as the values of
        # any remaining sub-requests.
        # Remove from the set of sub-requests so that this is not
        # included in future calculations.
        self.provider.sub_requests.remove(self)
        del self.request.sub_requests[self.provider]
        if amount > 0:
            grant = AllocationGrant(self.request, self.provider,
                                    amount, self.targets)
            # This is now a grant instead of a request.
            self.provider.grants.append(grant)
        else:
            grant = None
        self.amount = amount
        # Adjust provider and request values accordingly.
        self.request.amount -= amount
        self.provider.available -= amount
        # Adjust the requested values for related sub-requests.
        self.request.makeRequests()
        # Allocate these granted nodes to targets.
        if grant:
            grant.makeAllocations()


class AllocationGrant(object):
    """A grant of a certain number of nodes of an image from a
    specific provider."""

    def __init__(self, request, provider, amount, targets):
        self.request = request
        self.provider = provider
        self.amount = amount
        self.targets = targets

    def __repr__(self):
        return '<AllocationGrant of %s of %s from %s>' % (
            self.amount, self.request.name, self.provider.name)

    def makeAllocations(self):
        # Allocate this grant to the linked targets using min_ready as
        # a weight for the total number of nodes of this image that
        # should be assigned to the target.  Calculate the total
        # min_ready as well as the total number of nodes for this image.
        total_min_ready = 0.0
        total_current = 0
        for agt in self.targets:
            total_min_ready += agt.request_target.min_ready
            total_current += agt.request_target.current
        amount = self.amount
        # Add the nodes in this allocation to the total number of
        # nodes for this image so that we're setting our target
        # allocations based on a portion of the total future nodes.
        total_current += amount
        for agt in self.targets:
            # Calculate the weight from the config file (as indicated
            # by min_ready)
            if total_min_ready:
                ratio = float(agt.request_target.min_ready) / total_min_ready
            else:
                ratio = 0.0
            # Take the min_ready weight and apply it to the total
            # number of nodes to this image to figure out how many of
            # the total nodes should ideally be on this target.
            desired_count = int(round(ratio * total_current))
            # The number of nodes off from our calculated target.
            delta = desired_count - agt.request_target.current
            # Use the delta as the allocation for this target, but
            # make sure it's bounded by 0 and the number of nodes we
            # have available to allocate.
            allocation = min(delta, amount)
            allocation = max(allocation, 0)

            # The next time through the loop, we have reduced our
            # grant by this amount.
            amount -= allocation
            # Similarly we have reduced the total weight.
            total_min_ready -= agt.request_target.min_ready
            # Don't consider this target's count in the total number
            # of nodes in the next iteration, nor the nodes we have
            # just allocated.
            total_current -= agt.request_target.current
            total_current -= allocation
            # Set the amount of this allocation.
            agt.allocate(allocation)


class AllocationTarget(object):
    """A target to which nodes may be assigned."""
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return '<AllocationTarget %s>' % (self.name)


class AllocationRequestTarget(object):
    """A request associated with a target to which nodes may be assigned."""
    def __init__(self, request, target, min_ready, current):
        self.target = target
        self.request = request
        self.min_ready = min_ready
        self.current = current


class AllocationGrantTarget(object):
    """A target for a specific grant to which nodes may be assigned."""
    def __init__(self, sub_request, request_target):
        self.sub_request = sub_request
        self.request_target = request_target
        self.amount = 0

    def __repr__(self):
        return '<AllocationGrantTarget for %s of %s to %s>' % (
            self.amount, self.sub_request.request.name,
            self.request_target.target.name)

    def allocate(self, amount):
        # This is essentially the output of this system.  This
        # represents the number of nodes of a specific image from a
        # specific provider that should be assigned to a specific
        # target.
        self.amount = amount
        # Update the number of nodes of this image that are assigned
        # to this target to assist in other allocation calculations
        self.request_target.current += amount
