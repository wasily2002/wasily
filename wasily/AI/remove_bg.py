'''
Author: error: error: git config user.name & please set dead value or install git && error: git config user.email & please set dead value or install git & please set dead value or install git
Date: 2026-10-14 19:02:17
LastEditors: error: error: git config user.name & please set dead value or install git && error: git config user.email & please set dead value or install git & please set dead value or install git
LastEditTime: 2026-10-14 19:02:31
FilePath: \项目介绍\wasily\AI\remove_bg.py
Description: 这是默认设置,请设置`customMade`, 打开koroFileHeader查看配置 进行设置: https://github.com/OBKoro1/koro1FileHeader/wiki/%E9%85%8D%E7%BD%AE
'''
from PIL import Image
import numpy as np

img = Image.open('brain.png').convert('RGBA')
data = np.array(img)
# 白色像素 → 透明
white_mask = (data[:,:,0] > 240) & (data[:,:,1] > 240) & (data[:,:,2] > 240)
data[white_mask, 3] = 0
Image.fromarray(data).save('brain_transparent.png')