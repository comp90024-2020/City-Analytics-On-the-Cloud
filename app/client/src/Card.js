import React from 'react';
import PropTypes from 'prop-types';

class Card extends React.Component {
    render() {
        const className = `card ${this.props.className}`;
        return (
            <div className={className}>
                { this.props.children }
            </div>
        );
    }
}

Card.propTypes = {
    children: PropTypes.element,
    width: PropTypes.string,
    height: PropTypes.string,
    style: PropTypes.object,
    className: PropTypes.string
};

export default Card;
