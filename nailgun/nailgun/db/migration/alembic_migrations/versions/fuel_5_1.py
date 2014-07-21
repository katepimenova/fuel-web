#    Copyright 2014 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""fuel_5_1

Revision ID: 52924111f7d8
Revises: 1a1504d469f8
Create Date: 2014-06-09 13:25:25.773543
"""

# revision identifiers, used by Alembic.
revision = '52924111f7d8'
down_revision = '1a1504d469f8'

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import text

from nailgun import consts
from nailgun.db.sqlalchemy.models.fields import JSON
from nailgun.openstack.common import jsonutils
from nailgun.utils.migration import drop_enum
from nailgun.utils.migration import upgrade_enum
from nailgun.utils.migration import upgrade_release_attributes_50_to_51
from nailgun.utils.migration import upgrade_release_roles_50_to_51

cluster_changes_old = (
    'networks',
    'attributes',
    'disks'
)
cluster_changes_new = consts.CLUSTER_CHANGES


task_names_old = (
    'super',
    'deploy',
    'deployment',
    'provision',
    'stop_deployment',
    'reset_environment',
    'node_deletion',
    'cluster_deletion',
    'check_before_deployment',
    'check_networks',
    'verify_networks',
    'check_dhcp',
    'verify_network_connectivity',
    'redhat_setup',
    'redhat_check_credentials',
    'redhat_check_licenses',
    'redhat_download_release',
    'redhat_update_cobbler_profile',
    'dump',
    'capacity_log'
)
task_names_new = consts.TASK_NAMES


cluster_statuses_old = (
    'new',
    'deployment',
    'stopped',
    'operational',
    'error',
    'remove'
)
cluster_statuses_new = consts.CLUSTER_STATUSES


old_notification_topics = (
    'discover',
    'done',
    'error',
    'warning',
)
new_notification_topics = consts.NOTIFICATION_TOPICS


def upgrade():
    upgrade_schema()
    upgrade_data()


def upgrade_schema():
    ### commands auto generated by Alembic - please adjust! ###
    op.add_column(
        'releases',
        sa.Column(
            'can_update_from_versions',
            JSON(),
            nullable=False,
            server_default='[]'
        )
    )
    op.add_column(
        'clusters',
        sa.Column(
            'pending_release_id',
            sa.Integer(),
            nullable=True
        )
    )
    op.create_foreign_key(
        'fk_pending_release_id',
        'clusters',
        'releases',
        ['pending_release_id'],
        ['id'],
    )
    upgrade_enum(
        "clusters",                 # table
        "status",                   # column
        "cluster_status",           # ENUM name
        cluster_statuses_old,       # old options
        cluster_statuses_new        # new options
    )
    upgrade_enum(
        "tasks",                    # table
        "name",                     # column
        "task_name",                # ENUM name
        task_names_old,             # old options
        task_names_new              # new options
    )
    upgrade_enum(
        "notifications",            # table
        "topic",                    # column
        "notif_topic",              # ENUM name
        old_notification_topics,    # old options
        new_notification_topics,    # new options
    )
    upgrade_enum(
        "cluster_changes",          # table
        "name",                     # column
        "possible_changes",         # ENUM name
        cluster_changes_old,        # old options
        cluster_changes_new         # new options
    )

    op.drop_table('red_hat_accounts')
    drop_enum('license_type')
    ### end Alembic commands ###


def upgrade_data():
    connection = op.get_bind()

    # upgrade release data from 5.0 to 5.1
    select = text(
        """SELECT id, attributes_metadata, roles_metadata from releases""")
    update = text(
        """UPDATE releases
        SET attributes_metadata = :attrs, roles_metadata = :roles
        WHERE id = :id""")
    r = connection.execute(select)
    for release in r:
        attrs_meta = upgrade_release_attributes_50_to_51(
            jsonutils.loads(release[1]))
        roles_meta = upgrade_release_roles_50_to_51(
            jsonutils.loads(release[2]))
        connection.execute(
            update,
            id=release[0],
            attrs=jsonutils.dumps(attrs_meta),
            roles=jsonutils.dumps(roles_meta)
        )


def downgrade():
    downgrade_data()
    downgrade_schema()


def downgrade_schema():
    ### commands auto generated by Alembic - please adjust! ###
    upgrade_enum(
        "cluster_changes",          # table
        "name",                     # column
        "possible_changes",         # ENUM name
        cluster_changes_new,        # new options
        cluster_changes_old,        # old options
    )
    upgrade_enum(
        "notifications",            # table
        "topic",                    # column
        "notif_topic",              # ENUM name
        new_notification_topics,    # new options
        old_notification_topics,    # old options
    )
    upgrade_enum(
        "tasks",                    # table
        "name",                     # column
        "task_name",                # ENUM name
        task_names_new,             # old options
        task_names_old              # new options
    )
    upgrade_enum(
        "clusters",                 # table
        "status",                   # column
        "cluster_status",           # ENUM name
        cluster_statuses_new,       # old options
        cluster_statuses_old        # new options
    )

    op.drop_constraint(
        'fk_pending_release_id',
        'clusters',
        type_='foreignkey'
    )
    op.drop_column('clusters', 'pending_release_id')
    op.drop_column('releases', 'can_update_from_versions')
    op.create_table('red_hat_accounts',
                    sa.Column('id', sa.Integer(), nullable=False),
                    sa.Column('username',
                              sa.String(length=100),
                              nullable=False),
                    sa.Column('password',
                              sa.String(length=100),
                              nullable=False),
                    sa.Column('license_type', sa.Enum('rhsm', 'rhn',
                                                      name='license_type'),
                              nullable=False),
                    sa.Column('satellite',
                              sa.String(length=250),
                              nullable=False),
                    sa.Column('activation_key',
                              sa.String(length=300),
                              nullable=False),
                    sa.PrimaryKeyConstraint('id')
                    )
    ### end Alembic commands ###


def downgrade_data():
    # PLEASE NOTE. It was decided not to downgrade release data (5.1 to 5.0)
    # because it's not possible in most situations.
    pass
