from app import app
from opportunity_feature import install as install_opportunities
from estimator_feature import install as install_estimator

install_opportunities(app)
install_estimator(app)
