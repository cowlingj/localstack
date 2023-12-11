# LocalStack Resource Provider Scaffolding v2
from __future__ import annotations

from pathlib import Path
from typing import Optional, TypedDict

import localstack.services.cloudformation.provider_utils as util
from localstack.services.cloudformation.resource_provider import (
    OperationStatus,
    ProgressEvent,
    ResourceProvider,
    ResourceRequest,
)


class CDKMetadataProperties(TypedDict):
    Id: Optional[str]


REPEATED_INVOCATION = "repeated_invocation"


class CDKMetadataProvider(ResourceProvider[CDKMetadataProperties]):
    TYPE = "AWS::CDK::Metadata"  # Autogenerated. Don't change
    SCHEMA = util.get_schema_path(Path(__file__))  # Autogenerated. Don't change

    def create(
        self,
        request: ResourceRequest[CDKMetadataProperties],
    ) -> ProgressEvent[CDKMetadataProperties]:
        """
        Create a new resource.

        Primary identifier fields:
          - /properties/Id



        """
        model = request.desired_state
        model["Id"] = util.generate_default_name(
            stack_name=request.stack_name, logical_resource_id=request.logical_resource_id
        )

        return ProgressEvent(
            status=OperationStatus.SUCCESS,
            resource_model=model,
        )

    def read(
        self,
        request: ResourceRequest[CDKMetadataProperties],
    ) -> ProgressEvent[CDKMetadataProperties]:
        """
        Fetch resource information


        """
        raise NotImplementedError

    def delete(
        self,
        request: ResourceRequest[CDKMetadataProperties],
    ) -> ProgressEvent[CDKMetadataProperties]:
        """
        Delete a resource


        """

        return ProgressEvent(
            status=OperationStatus.SUCCESS,
            resource_model=None,
        )

    def update(
        self,
        request: ResourceRequest[CDKMetadataProperties],
    ) -> ProgressEvent[CDKMetadataProperties]:
        """
        Update a resource


        """
        model = request.desired_state

        return ProgressEvent(
            status=OperationStatus.SUCCESS,
            resource_model=model,
        )
