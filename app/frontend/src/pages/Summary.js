// @author Team 42, Melbourne, Steven Tang, 832031

import React, { useEffect, useState, useRef } from 'react';
import Card from '../components/Card';
import { FontAwesomeIcon } from '@fortawesome/react-fontawesome';
import { faUser } from '@fortawesome/free-solid-svg-icons';
import { faTwitter } from '@fortawesome/free-brands-svg-icons';
import LoadingBlock from '../components/LoadingBlock';
import { getStats, getGeneralInfo } from '../helper/api';
import Moment from 'react-moment';
import {
    BarChart,
    CartesianGrid,
    Bar,
    Tooltip,
    XAxis,
    YAxis,
    ResponsiveContainer,
    LineChart,
    Line,
} from 'recharts';
import setInterval from './interval';

function Summary() {
    const [loadRequired, setLoadRequired] = useState(true);
    const [stats, setStats] = useState(null);
    const [info, setInfo] = useState(null);
    const [lastUpdated, setLastUpdated] = useState(null);

    // Prevent updates after dismount
    // https://stackoverflow.com/questions/56450975
    const mounted = useRef(true);
    useEffect(() => {
        return () => {
            mounted.current = false;
        };
    }, []);

    setInterval(() => {
        getStats().then((stats) => {
            if (!mounted.current) return;
            setStats(stats);
        });

        getGeneralInfo().then((info) => {
            if (!mounted.current) return;
            setInfo(info);
            setLastUpdated(new Date());
        });
    }, 1000 * 10);

    useEffect(() => {
        if (!loadRequired) return;
        setLoadRequired(false);

        getStats().then((stats) => {
            if (!mounted.current) return;
            setStats(stats);
        });

        getGeneralInfo().then((info) => {
            if (!mounted.current) return;
            setInfo(info);
            setLastUpdated(new Date());
        });
    }, [loadRequired]);

    return (
        <React.Fragment>
            <div id="summary">
                <h2>Summary</h2>
                <div className="row justify-content-start">
                    <div className="col-3">
                        <Card className="card-disp-num card-green">
                            <h3>Users</h3>
                            {stats ? (
                                <p>{stats.user}</p>
                            ) : (
                                <LoadingBlock>
                                    <p>12,345</p>
                                </LoadingBlock>
                            )}
                            <FontAwesomeIcon icon={faUser} />
                        </Card>
                    </div>
                    <div className="col-3">
                        <Card className="card-disp-num card-blue">
                            <h3>Tweets</h3>
                            {stats ? (
                                <p>{stats.status}</p>
                            ) : (
                                <LoadingBlock>
                                    <p>1,234,567</p>
                                </LoadingBlock>
                            )}
                            <FontAwesomeIcon icon={faTwitter} />
                        </Card>
                    </div>
                </div>

                <div>
                    <h3 className="mb-4">General statistics</h3>
                    <div id="front-table">
                        <div>
                            {info ? (
                                <React.Fragment>
                                    <h4>Tweets per hour of day</h4>
                                    <ResponsiveContainer
                                        width="100%"
                                        aspect={4.0 / 3.0}
                                    >
                                        <LineChart data={info.hours}>
                                            <CartesianGrid strokeDasharray="1 1" />
                                            <XAxis
                                                minTickGap={0}
                                                dataKey="key"
                                            />
                                            <YAxis />
                                            <Tooltip />
                                            <Line
                                                type="monotone"
                                                dataKey="value"
                                                name="Number of tweets"
                                                stroke="#38567b"
                                            />
                                        </LineChart>
                                    </ResponsiveContainer>
                                </React.Fragment>
                            ) : (
                                <React.Fragment></React.Fragment>
                            )}
                        </div>
                        <div>
                            {info ? (
                                <React.Fragment>
                                    <h4>Tweets per day of week</h4>
                                    <ResponsiveContainer
                                        width="100%"
                                        aspect={4.0 / 3.0}
                                    >
                                        <BarChart data={info.weekday}>
                                            <CartesianGrid strokeDasharray="1 1" />
                                            <XAxis
                                                minTickGap={1}
                                                dataKey="key"
                                            />
                                            <YAxis />
                                            <Tooltip />
                                            <Bar
                                                dataKey="value"
                                                name="Number of tweets"
                                                fill="#38567b"
                                            />
                                        </BarChart>
                                    </ResponsiveContainer>
                                </React.Fragment>
                            ) : (
                                <React.Fragment></React.Fragment>
                            )}
                        </div>
                        {/* <div id="table-left">Left table</div> */}
                    </div>
                </div>
            </div>
            <footer className="text-right">
                Last updated:{' '}
                {lastUpdated ? (
                    <Moment format="hh:mm:ss A">{lastUpdated}</Moment>
                ) : (
                    'Never'
                )}
            </footer>
        </React.Fragment>
    );
}

export default Summary;
