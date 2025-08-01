/**********************************************************************
 Copyright (c) 2020-2025, Unitree Robotics.Co.Ltd. All rights reserved.
***********************************************************************/
#ifndef LOWLEVELCMD_H
#define LOWLEVELCMD_H

#include "common/mathTypes.h"
#include "common/mathTools.h"

struct MotorCmd
{
        unsigned int mode;
        float q;
        float dq;
        float tau;
        float Kp;
        float Kd;

        MotorCmd()
        {
                mode = 0;
                q = 0;
                dq = 0;
                tau = 0;
                Kp = 0;
                Kd = 0;
        }
};

struct LowlevelCmd
{
        MotorCmd motorCmd[12];    
};

#endif // LOWLEVELCMD_H