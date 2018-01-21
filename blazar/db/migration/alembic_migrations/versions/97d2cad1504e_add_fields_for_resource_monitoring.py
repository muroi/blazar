# Copyright 2017 OpenStack Foundation.
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
# See the License for the specific language governing permissions and
# limitations under the License.

"""Add fields for resource monitoring

Revision ID: 97d2cad1504e
Revises: 6bfd1c23aa18
Create Date: 2017-11-01 13:25:20.479355

"""

# revision identifiers, used by Alembic.
revision = '97d2cad1504e'
down_revision = '6bfd1c23aa18'

from alembic import op
import sqlalchemy as sa


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('computehosts', sa.Column('reservable', sa.Boolean(),
                                            server_default=sa.true(),
                                            nullable=False))
    op.add_column('leases', sa.Column('degraded', sa.Boolean(),
                                      server_default=sa.false(),
                                      nullable=False))
    op.add_column('reservations', sa.Column('missing_resources',
                                            sa.Boolean(),
                                            server_default=sa.false(),
                                            nullable=False))
    op.add_column('reservations', sa.Column('resources_changed',
                                            sa.Boolean(),
                                            server_default=sa.false(),
                                            nullable=False))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('reservations', 'resources_changed')
    op.drop_column('reservations', 'missing_resources')
    op.drop_column('leases', 'degraded')
    op.drop_column('computehosts', 'reservable')
    # ### end Alembic commands ###