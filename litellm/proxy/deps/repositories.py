from typing import Annotated, TypeAlias, cast

from fastapi import Depends

from litellm.models.deployment_warning import DeploymentWarningIgnoreStore
from litellm.models.gateway_activity import GatewayActivityStore
from litellm.models.session_topics import TopicStore
from litellm.models.user_banner import UserBannerStore
from litellm.proxy.deps.database import DatabaseDep
from litellm.repositories.deployment_warning_ignore_repository import DeploymentWarningIgnoreRepository
from litellm.repositories.gateway_activity_repository import GatewayActivityRepository
from litellm.repositories.session_topics_repository import SessionTopicsRepository, TopicDatabase
from litellm.repositories.user_banner_repository import UserBannerRepository


async def get_session_topics_repository(database: DatabaseDep) -> TopicStore | None:
    return (
        None
        if database is None
        else SessionTopicsRepository(
        lambda: cast(TopicDatabase, database.prisma.writer_db)  # cast-ok: Prisma's raw query interface is structural
        )
    )


SessionTopicsRepositoryDep: TypeAlias = Annotated[TopicStore | None, Depends(get_session_topics_repository)]


async def get_user_banner_repository(database: DatabaseDep) -> UserBannerStore | None:
    return None if database is None else UserBannerRepository(database.prisma)


UserBannerRepositoryDep: TypeAlias = Annotated[UserBannerStore | None, Depends(get_user_banner_repository)]


async def get_deployment_warning_ignore_repository(database: DatabaseDep) -> DeploymentWarningIgnoreStore | None:
    return None if database is None else DeploymentWarningIgnoreRepository(database.prisma, use_writer=True)


DeploymentWarningIgnoreRepositoryDep: TypeAlias = Annotated[
    DeploymentWarningIgnoreStore | None, Depends(get_deployment_warning_ignore_repository)
]


async def get_gateway_activity_repository(database: DatabaseDep) -> GatewayActivityStore | None:
    return None if database is None else GatewayActivityRepository(database.prisma)


GatewayActivityRepositoryDep: TypeAlias = Annotated[
    GatewayActivityStore | None, Depends(get_gateway_activity_repository)
]
