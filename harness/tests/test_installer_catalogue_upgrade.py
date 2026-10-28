"""47→50 commissioning: root-owned/local fixtures, no service/provider calls."""
import asyncio
import base64
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import zlib
from types import SimpleNamespace

import pytest
import layout
sys.path.insert(0,str(Path(__file__).parents[1]))
import native_executor
from native_executor import NativeScope
from native_install_client import InstallerClient,BrokerPluginInstaller
from native_install_readiness import NativeInstallerReadiness
from native_packages import NativePackageStore
from plugin_catalogue import catalogue
sys.path.insert(0,str(layout.ROOT/'tests'))
from test_native_executor import broker as fixture_broker
from test_native_install_broker import Backend
from test_native_packages import package,Fake
from native_install_broker import Installer

HERE=Path(__file__).parents[1]
VIDEO=layout.ROOT
DEPLOY=HERE/'deploy/native-executor'


# Exact source bytes commissioned before the later installer compatibility fix.
# Compressed fixture payloads avoid requiring repository history in shallow or
# archive checkouts. The real historical upgrade verifies their pinned SHA256.
HISTORICAL_COMPAT_SOURCES={
    # d1a73a869b34ade30a4cb5c529ef9b827b6f5c53, before the reviewed retention upgrade.
    'native_custom_broker.py': (
        'c-qZ8X?NR3lHd6&VDG*K;DFHKE1?FHRct!Zi!6C5DaYz*@gSfnVgv#R4#^y^{`*$-g>GD;Jehf`yyp;&KC0`k>S{cH{`^DynM6id'
        'msPf&79@y_zh&2@5vOGw5u;iWBQN5+I3YLCEe)#pju_WPc1sF(GC7CWK@FYKDh`7RdgKMUi^;ularEA(iXbhQq$rK+tWG0%TV{0;'
        '!uU-^O2<ga9Vv{!2($G%NF!$wuY()n7^|$Tj6A4TMpnRt*&RSq7-2yoTqj6e<EQiUPjum|h^vhemoQ67@}K}1*zg-zyK*PbpFf{0'
        'i)?N9{<5y>g804>uk);^j37<33igww<wP`<n>36w@wf`gRT5u|$G??XDt>1rn--yjE@IA4@F=<zMEoiVz+TB!%2i#(iG2Dai7WC_'
        'J_1PMcNMS6L_9DG`S)3Ii%2UC_Dj5Gl<~=D64q5lJFuo!hdc;x0m=RvD1sX{ctbgv{BnAH@p10|@ca4v%zN|d#oq2d`)Bg;@Hc-k'
        'KR-M=nZNhGrH?-yot>X9e)slwUwu3I_3-%EKbyZhJ$ZlTy?Q-4UmTvCeV8v~+soa_gd*37$kK?4AWp3+C~xiA#DKs174Vk<<S?+&'
        'jk1(j{1yIH#by@V2eBlOTdrb~L~IJ#jeHJC-t>7Fq#;QX63vSuD`sH^ypz_1b_a6QMaotyNLeS9WvdXD08d)^oH4BzL2+~EAUU$S'
        ';(!5XU8Ge3R6+59$ze~(rNB-ZYz9~|ipwx4fb9>AAW3jd>=GF;M_yCzGXAK61woE*!b2kjG(^Ta*g)SPTp6E^-tWl?BT|M%oC9r3'
        '1LP(Jq`^NZpZ7(O=dd)$M_iB)7!9Y3s}jT~iqoLj09Qq@s%xB3i3@`qMZtKVg}_16qD}=&amu7;P1aelam7X_f(jDXAeA90OHU^Y'
        'usH~zU)I<BJ>^I1#X88XvML-zqwN4Naq3A@%Fgcl`7b9I$H$Ib1^*c6W8I+#DT=KpB<Y>^#r$-5{*yT~cj~g(DX-&nMt{*C>=FJk'
        '9rO6`<VWcC&b>JQF#XniVBE$z?evVX7O-<kxPZBHE`juwWpc3v93%VqbsF3SaS~i7#I&gfIRR#$^m92r_)C~Y><1exTQZ^wi`CEe'
        'j%Yq-V`!rdoJT0+5;5^okkn<e5oQr(Vgzrd;Sb>yeQ)C=v1}c^44zM{$8nb3);U8{9+ai*%67^$L5qD*DqCB&Z57hE1nX4cJ}ol5'
        '$!*BObg0h}+NKEA6gbIb5JgW|WC?U9kqLSxOaK$cJNi}(VK_$J<NI+6boZ^2Buj^xTIYqsXF-GGA%xB~C~lHQ^g&UUILc+CJodi>'
        '+Eip&#T)4F9S)w^ymr^Oph2uP?91ED-tO)?<a1nBes=4f7d5f<HB^Am?paC$8gAMKW*2tt`?h-@S1Uga)})0OH{X~ldDCU)wdZ#e'
        'G>ewT1ZXX=)jji%&KC0@Egb0rv<SWp>x<_)j-Gj0>E4ivz;8=^hkgHJH{0FcA0=d*-UUe<`5NG6KR(J;u>bmv$9)HvJzAX|p{M0j'
        'KM1(nU6IdG3`SVpBO@dyGXFo~mGKSJ-Ik=?j(L|Q^_rBX(_X7JWpwRvpTh;)l7hidXF=!B<?l@=2Gc`bBtHSmkuY96;M2f}d*JF2'
        '@U)RrF0%XfaxQa9fbgFdhd+Ki1P2qOCrBvvJ@6gOX3BO~6zo-zrCD8iA3&9lbX^CZVY}ekgy`CM-|l_4Z`#AKnC831{P28koFD#h'
        'JU5O$7$>La#{4(%YtE<(Y{ijrKL739__R3sc)0l8_<8=jQ-)bi{9CeNuW$&pFa`Oe2J-o0jC`T1PLRH|e`Ko_IlC@fjZ8mA2Ecu+'
        'PJsRk?W(VKadPxu7jsL3tN>}-u>Tfv>O9GUsPw)(aE(x>$^)<~IP&s2Oh}N{xt{<V(pIinxP%BdluGhMs<s3@P)Z)ka-gD_AJ!($'
        '^RxN!{N1_1y1uZD4~x@}EH3%=r}<)TaP%M!j!q2AEbA~N5FSKevbmmhOn?OZBK%bl0JlWt``mVyqzYHm!@S(RYP2peGt;<$sX@R@'
        '4%mwhL~-POZ#ovD*;dzR7GN;TH8_bm`M4JEtatJ0J@P1B=xlz@H|~8ehJCLu&*#KQ#f|E9URtc54GNR<3D|U0ow%Ms5>R>6nBb;W'
        'SW&xFK3ao*oCrfwr#{siLa9&9Z?JDO6Gc3m1TZWo`C`VAIfIr<{Bh-&R5W-C`wfkfb@_|t8DB~m%^5hCOo^$G%h_%Fp#g|$au6d1'
        '6P?kaK-#mmc~Sff+QW(ButM`ul8Uw1&pf&l0lx~e2I0d~><gV*5l`3%Gl`T*>07=r-5FBe0CyF(ByW3TFb!IgHcp>;^ua~-2CwOK'
        '&-luWj*NtlyF6^tbzH6k5CnBAjQj<+5|ko&eVxQ%ACEA07Q|-{KzX5jz<e#^Vojn}kC=|#QYAb*F#Xg7e|dqK1dTx&&FMs02xc~|'
        'M%hK_kAwhZQnV9yStkj_iB*`F!F0Jh{qE|^%l!vaRnAZ06+TQsG5Y7+wc;-=33EGEwtfkcny^UWe3O$cJm;bHyRI|CDEfVx&Ono)'
        'M-*8SdE0AR$*`dO4o}`24dR#z6fCTnyS7GFNC&Iy;P7^;M5#%%pv$Exc6tRuFA%y_=*PhbS3!D11Az8os$%7W1}k;|)sWa&Qv0tS'
        'Umx=_zn(6Bo-fYE0SodVjH``L(+nMu#8OW;$j`h+uG*INub!Gk+6HkfC*ccdABWN$gwpH<hUc)hfn^185V*NRz#upBET2m)fe#+N'
        'wf{=*)q(fu<ZQk`7v}twA@i5R<BR#3^}VD1hhCn|eTzQscDgIS+_zOHK+3;ka^(gp5QAP9|Ixy0{QEcKeO+LR0^+0!hM`6bL6w#?'
        '-B}s|1R_DYF-rJxoqfjaRu<AQ!PVxWA!jSLFn0j>gZ^j}0ZXfr1)B#J8^lt^K)`l)*&|MKjNQ8(bmUWGTzVZh0sWN&{<dckh#Y}$'
        '?o*3*aa1u3@*LyKPTNh~65k#XZ!9`<zFJm@x#Jx##bKVfb6-HXRVYSiYi-+j6OAjRctLF;(=00pxMZMh#0IRMF9-ApgK&x!M-W*p'
        'zKhcv5$f@{<WlyzlBvG1E|6nb#;BPdrBU`i8013DVrsA{xD-qA6p__VZQEVj!BSfsFF#N+oRQ)Vhye;WPc{~#ozwU?%BCN>=HWI@'
        'Bc}-NJ#Av2W)MU=e_3E=Meqi*{_juUU3{FMoU@GnH@x+5@uS-M=9S%N{BX@@-s`7$0u+QE>xS69LpQ_Y5M0-3ln_g?udq3;ulO?('
        'ud%mIn5}bAINUwuPpJ7oE=j?g>2GYLr%1vkLg@bE+|hG0-E>VC>M;k&`I&6C&oP#1ke4eEgvK52`by$&iOH#otT7W$kp{BI$D3D|'
        'CMMHB47)r>5gimar1CVixr38kny9xy6j%?J>Dh7pJCyp<mD)OpQ{Y;Uty0E+CoHE0YheNmXw%Ar^-n}4jROayOfB>CwTW43aGpu9'
        '?%!^6a3tXv?kMF5^j)yEAg$xndh`1A%Quc(!nQ}(kfQU(;7&g-_-BZbekOuwaj71$Z~>d68<Ztg=&VfwJ@pbmDs7xRGUqa>%aujV'
        '-V#-xCED>sECW}8z6n>rHtko=ZuaWctNs-LFC0Q8)%i^D4do4B>usco@^xQm`SoPp9Z`XAk&b^B4Y|X@6rhU=58M_?MV<=g8Zr#h'
        'ERDk;v8WEPrK_WlWK`JA-t6}5K%DEkC4qT#U#e@ewsjub-gmzbp*akVZQF{zJVNhhBh6_*FWAK0jRjSTk>y<-sZqJ|H)q49=d#oJ'
        '>Y*J({{UN_)1Tl0&w7It>E<<<@ifeIH&&-1De%O&wYTO#irR}f1@C(^Q#~a&&vH5k%_%n7S71v*KtnS}R!Qtlx(<8CL8j+2)NOz4'
        'Eav^uV*D(o$ANx|a5az-di0>+NBZgrsH2|wXdXJB;ikyy+-9+e5T)09yV}ma<Sw3BQM;U@QKr4oeM8LH519Ysna@>K<#g&tl70F%'
        'kkw+{JEkXU>JgnXHKW5;XJTik?|yzVAq=HYn#AetGjH$d#OWgm!rVs%>b4D%$600&T;BWTW{uF4-iIIo1;e5;BpIESs=$G+?YIi?'
        'Q&ios3IqG=IDyc9G_luo8%yjRPX9ZY{(X1)oj=9wp0l_A?SrW;@dT7M!xXgsS<&8s7Arj#`*98W1Uucs-UE6Z$wH&PPO-scz?t}}'
        '<JD(9-v1L0@rRRmY)iU|rJ8d%3&Nrz#rX7Ed2K5P+%$Q^CaVJ(O_Se%qUzW^KB&jYIVMB`#U{_<v@&Nsk;pno<0W12Qtf)>^xAE+'
        '%CbaRk3Pa8;PTVNR4LbJTHy5!u<L!W@%iCUBMA>Z?v7NNG#|86)G5Q@0cD6$FN<LauuHoNTY%1NJI5sL#Ov2XETD2V0Ff%G3_Mc3'
        'gcnK%Ln(RTo<g0ngivdU8$^+hm2sdDk_77ktRz@pM}bkzDwhcV!?L;3s&Id-63A*!f_iIpbO&bC6*<t|kFJv?cHB8tp&RS*IpjGF'
        ')7L#ke`+Cw%hBWX36#_8x&R{O5*W*lyKh>NlJKrND(SK6C9Vu0_<~@HLMA|H=UO!f4vGDk_r|i~%`q~UDb<S$?mc+`1iGUjH+2&5'
        '15$xURxLu6CB@Q?i9<?=8psYiDQ40ml1YpC*~KwW8mY&5^c1?4$~oX;QrNMjor+;^c8ZFeB*APXksnuUGhpnB)BeR9g@tN>KDO8~'
        ')eTLwqyrWBzLbL&V$)6iDFLz;sx{IRt{_zR??_Ps51O+*=V`k7G40x&r1Pq!xH-z?z?Qv5gs4{45&c>AXC_F7=M5Qd-xNoZpBASd'
        'j*jP!Nwai5o4}lD**a6R&OA8M+Pd0>%UV-ip{tdVM7dVV0?~2>^95KDjME6=K8BY-+acddgm<G3)YFco`6+m-7MAw&v}qxbr0uA6'
        '50q+>$Ni`t^@=lfcOpVr0XsbbV!;LlXdoX6YjSuV)0Z;Kp83@Zm6oNsQ%vVkx(@q2_CRW`CzHzycV!;eS%Aw&WlyAa2hjL<OfN|>'
        '6U0KBb1*hx4;ku|*5~(A;_(9wHJi+e%7OPRk^XUnH#0;wLYOB-hYqEGu$xPh#HD2qCteW*jG!Ig@ezs)OL~)A3y0e?8OG5rJ*RdW'
        '_tSe5Zn4I(Y=W)}Hg?L_OG<w5CwC$mgr`Rj0rjPM7yF;j8a;86)E`Yr7kYP6hae2>Cu<i<hFjLh{l6`M=s1Mqv6IlS%CP1Hq!XTl'
        '0vajJ`WFPgL5O?tE-T8s^iss7mS4N!p`Ft>ZL;ZwS}rM?9V{5L?JE=;syk&FUjIqvcp#3fB)MSL7fSb~P)X9Bho&~rCXL&nfd#s;'
        'I>i>ikCQSS&gcaj7KiAWD*nFDE|9duds^(emTZMka77C11smuv<LJw<hZhYfCg7<qx;j=ntyjv^I>+_Y_2Bn>`huGgg&?v21drgW'
        'L~XLY-Jl$jRRd~$NR{J|EmXO#L+PN&NaQR_cH0_)zZq2$jYVbpq_4hVOp}Z$4)#tEU9H9(3yZ~edaD<acOSa@>?{B1)O~DQ&e507'
        'YqZGr<yhJcrh$?R6tICJ4XvEK?QJtPiJ6Yp)xQ4^6&gXb>8sAr4;fjSiJdO;NnSghD7WjxqHU%=?4<W>Tps3OsLFGzR&NSKi*+A6'
        'Lg&Zl!TrqRp(<rMdgN`-9^Kg+tV;O`jJ$QO)!PSp3WRY6OL++)W6aQz50Ib`Ho)%^G|U3MOD_QxW;F(yrWY}T`c3mf*z@KcxA{O6'
        'ya8=!yefK0rvQ63mRhxb6oO&n$6i0(_R(=$m_EJ*xr3fU&KFOp;0*AN-)yV2e7}D&YT;TAhEfSO;AXTGyfEQSwSP-PH!u2p-#(Sc'
        'l7KCCBcJoQ=<wRtpqk&(U^iEqdQ5qgjYZA;s?T4(w@(*_+P^XEDuSLnmQv0+eX}bsq4W@M^vDNm|HWpweBha&+KlpK&|45)PuJG('
        'S*!dq=0_g%5R}zrYNYH0aU!<wqoRoN?N(0r>xE;ByGA<kT4Z^-J4zSfEUGANnD;TnZpZH1V5-IVtALj4QBMTSPeBWP^?MP6MX5`?'
        'reAtrOgT_okue|s*P`YgEAYijh-})2o-rv+dV2B2Y$~3qTU>!Snh*WM(KdiN?;R<Mp@+tDi4<emWgN#;j$sD|(j!cLnBp(=dK(Ns'
        ';<|{#9yMF>7;boJWp56-anz`X`Oiwmwlc>_ypHie?+ed2iJUNB^X&9u@s59G!Vz6qU1O^Yf_B!<EcYA%0Ei$>eu@4-1YOjxQ8@fs'
        'tQf9-<HEthGf=(!Y`2sfSME_fDAhj6IKOa8D4I-OBvNJNQsu_0rd6_~BDh0K3ifX45Bhl&;Gx3nU8}8ZTBQxm@U8DGHAAp_waMkp'
        'r%NHAS2oXdTxVJGk6mnfa-o_GjsDzwMar%R?-dDusEv)mYC39?`aZYt#5*tlvMVq8g^>m0$PN8^3I3{rTVe!YK$2j?J`04Rz>P`a'
        '8iym3Mf~=OxEc-6TmvjAp<frmQ!66@16wE;PYy<=OEdA^Az3r#@Vqa&G5m`<fP8MZ-Qf)MKXid-w4~SSy*}jhaoxauA2S6xRTD-d'
        'H+;583T~g48b{aD9D8BSnE9Nw@1Al&!4#@~`U7zb-M%bzcwBEY@3u#6i<>vP)@Bf0wOpM?S7A;;cM)EOHnawBP@Imdt=6K+3bt5J'
        '_Og{BYOiZ=vqYBn?nY+WI+Jc;%{a9?)5=ueSI>+D)+QV#ET`x}>)aJNYE)g;>1~?br+(w&wv*70Cz@E9xBg`q_Jxn3@50auSN27y'
        '_QzlNhd?|DZuROH*2L;!n`;`L>JIT>i_OWt=)&i3eME!8<yq!VmRCDP7G_m8Wou2vcOQ0Gr|JH8?ziqfe*&#Bo#N-EOguq~KhJh{'
        'uu7{n1?$U)u%cH6z<(lp$Kje(t1OaO;qD!1NK5@lL91Rt7_}MuJb_E-$9wv|ywbl4{cepqzo`SQe*B;f8ZEKu|B3>q6iebMEpj_M'
        'COro>+o><5l!|i#Kay-%fz6~!lUQvnKfuvmuFbgZ&!8#vr?qXAqKF&*(HV&*Y=_8$pgn@oF2k0qVaux^CTUqk7{<SpWjdUF7Z)ez'
        'M<3^OB~qcWhB;Hv@N{wZp)&aSdT)QNv`owUAXl5i95kb2?$L)lj>;*GQ{eH<s~#XgPfW8k*|4!eK8=bD=Y!CKHkDW~0BU&(GC<)0'
        ';O?jLONk}zRF{<L_jY%8_h$58+MHxJFkO5{U$ULF*RR(366e@~AG6}#NwX5KnWn(BcT|48y*F!zUftSz`}Qr{IOP;)X`&zQUE?j7'
        'vIxB|jK&W;vcAiS%F1gVyQ>e)gq-%U!+a?=3IC1glGqv_O-m2hVD_^ayDb=;*B{s{jRx5c3`NO3G?)O5BR@wq0+Lec3eofibVT}6'
        'aw?CPJM)?1gKeG~nc9N~-}(`v_FamS;DvvoLolSWEIr-cFRM9Y$bdSC)9D{|Oe*>d0`H3PA&9Q`KRf0(x*&gJ{<{fU5(2_FxAd>D'
        'v2`1uwt=Hy!nn+69g4ox=;YfReTr`|`h1w#MzN@>>A^DBma0;erI9LJ>|Jv}+M`~@D(ty$G{1k@l$>ZMqgp3>@_+JX>&5'
    ),

    'native_install_worker.py':(
        'c-pmEdvn`168~SHg2~Ms#F(Ze*GW^U^qlKBi6@SovEBB1J`aZ?Aqh1_vIOZzTz~i5T>unG(W=|Kb0)U%S}gYYTR<;gzI-'
        'q4cx0usSu7H6B|#x>xOH%RWG#b&=OT#3SDw4pS(#XA65m@Qks{)jD5UjUnHGU1gCv@!cW$qDl3L3&;;|J5aV#Ax3&QJQ&aF'
        'k7UPCj?dBl?fNJ=Zst%Mi1X@2cm7YlAp^YohMmMpU@7F=3E(i6*NSp?IVTR|Q!kSo~8;&(w<SULZ#<g%Dp#X?9c%hP!tEUn'
        'vI6tI@qEP&{Yj)t2^V1>KAmoH!TW_h}_e1BFJWzK!y63Z;j3!t6AVG5BZvZuQifn3C5YMQ^wG%=r3spw=ufl~~TFVHkoa&A'
        '7!MOlcrYQZkm=W+_Xhg?>B7C}A}G4J*E&kjBw{dDO6@XN*Fx%bV^tI=?0mwxrm5B5)V|Lf7(z~RZo*%1tlhWOL_vVZVt|AV'
        '13+Id@>|NiLM47}N`^_w*~e(t>;eYaC}Rn*^n^LDp)aQg1>pB@{OG9Sb#Xnimh$shz9xMRJ~`)8jHFTNb_9~^oNPMH?zb|#'
        'X9N83RrSg#kxft1$IpdOwd=6Ra;e+uG~(?@%p!C<c!@flcCk|qME_iu3A9_Ji%mykl-'
        's9a`J4LA}c=wI`D>0RX5<XMmhMVd>m&m8zSX12o<iFyxYD1`SOOp!Zatm&<v1c_?fE>G|XeVcZdi=efywhI?Vc3!{HgYJUg'
        'MPd#{3F~N~wo$XVXTgt{Szu<Ckl;l{kc3=w9*MB9(J3T13to5+Or~WXa^PHhfUmZ!bxiNU;@RUk4}|0mSu&9TArbksRUG@8'
        'A7&FLinK?2U7C%qo?uc9H@Q@O#AX(kbCLKW0>*NlrBZ;<@2PPERMG@Kf~gld$Fqb1u#?V9?pMRl;$QP<Z{+c^#>|x?i=6ny'
        'sX2FNWgJ6P3m5&IT?X4<_qYEw-2U!r_0e~?uO5cZXy@BU=5!FW|A&0m1htt_U=0}5+-'
        '(h7?G>MOf`6XT8SD<O1L583*Y(z+Ep5vij|ZgwNc=OD{lo5XC2CysmqcttV2tbvqDNlH{#w6U!Q-'
        'wqFFeNn%I3v=rrr3m<(5}ANJa@LDU2Y7-*?ab-fM(>Grb)uF59u-'
        'd18LE3u1nPz_UcI7wKc`NJs=#v{M3qPOPi5OFh_#d7_4^;A`*g40qpdV$GZbldw@o!Ix46HCF<XDEe%$(iCn&Gl2Fk?iwWb'
        'ePkoUMuxWkz+5nT*JY2`#s2+ohP|0;3|9>#mDPFO9g+(X5w*vkWL=At)+&+S0Q{QWo4I|Q>+rjbhXs$cjAxOTN;l1r8ytql'
        'sektF^yK)L-'
        '|6$o>HE{;<I|r_>*R;y(}Pc1BFQYp<uimN{aMtASlH*D`$y+zhadVl)0Ksf=#F$}IwXL@7oK5i&8Am@0=5a`RC3&A(jgeLa'
        '496&uE|p1n!%EojOdiJ2(rqQ*^`CWO2cZw?E=t~6+zG%dup)@zoPz|*RQ{MV~-'
        'bZnZY3M<BaOOi+b|zI*##_)p;~+b*4XmB3E*L-5<h2rFGS-'
        ')XwjU95Ajgc}#5dHKj`TP6UjKgutq{0|2(?M8YzwEFISf4Le|6ze2Z2<`%&GYdMG@n1_V3BP&==#k@>Q2vxz7OBYcxg-'
        'N}iXOK)~d=rfxR+2IRVp8MHLpa>8Gph_eO$)CpY1KUTas-'
        ')en{BP8>D;3bN|tlL_8wB&!ueZk!MrmoK;Q>0F{2626AzIL97hYyGy!pXP^siliFgH+1+j;qTzW%VSUZusypBZPN5Alf>9@'
        'n7191>mY6Qyf1UN~0t%0SoP*E(}uHDwKZ3R-'
        '>grh40<>dG&o&Ks#+o63~{`>qZqb1WaiDJI8QjAC#P%tR9$4JBKj9cD_&b;N}WEY*fF%4v?XYSQY3_^miY;RSNSb9UE@%?b'
        'T!7p&7AB&|hh}8U`5h_OTlbgd?MMhSEjL?9PD0N%uvzpqVqVf@T+wHSp=L<3(GJlMAv=f9pScCfMlV#zSn@)g}+V0)j1_h6'
        'H)<q^N!Q25}@hTgjkndZBaJ3sm(bGrqJmn5e)z&j`O=O#rk}lWjTrDo}CrqJsPs%mZ=#rn}NJ+4BUCv{=Y4C96C1q1uVsDM'
        'cR}-'
        'Z?Gsm6);W;n7n&S=3U`trak4a$F@;1Y`o0)0vwJO^#0U5!NemZToWtd~d@N=UBw(Q9!`D)_m{(ZZ0G9lySy3v;R%WfT)30|'
        '#0eau&S?lL!ua~KaO>}?1LG;yzSQ_yh!z18Ic_7l0i#%07NNh5_$L2JP(IsPg6HpoyARuR$F`Icmh?5p5Qxx-'
        '(@VBD2LFuR+oV4IdjRu=1Cm-A&H5=gaA2yX>bs+sFVD9-'
        '>!@fwUMEVC3zzgp@jWMg+y(AGn6XhR?q0ArU~%nQ!C>8PW+bibyc)1+cx+e3d^;i-'
        ';wv$&KC$g6}k?~|}^`auXu3h<dm!UZU@J>-yHEB``O|9zN_Mk5vbhv}Q)uz_32E=_q+2Ok6G(#6!as^<slkBgT`%m&x-'
        'm2+*f%4Lv<8J86Fp{kiTk^2Lqz1f)lI80&SF<Jhjf_4Z)K-'
        'xE8qkwoQ%NQU}_~H`qlgJ@3r35{osnEx;D$x<;Q&J&U?i0cyGHyPP(`lc*Vs^bgM~IJW+?YVVJ)w+2JtjW-MXE^z(nmCQ2h'
        '+OJCqgPy6}&q-YgPs08slz|fv2=nma4j+E-'
        'Nq>o1O$ws^^WGj$vX_t@v1r!nLvgQwSnenIk?hD&fX>REN<Tt@k=7Frq4mG2SL>=?9UT3ij_Xe#b*~+s($)G>sdwQ5J7M)@'
        '3RAABA~cxx=S+pw~H&4q~sz@YUEBx%Nu2|7Es(!cl4)u(}y(r>VU|R6sdO!*U55IMwC8^U6^&<lgF5-'
        'OWo=A(<6bF=IOHNku~yELF2AbIjCQnX?Sw#D!PQ9Wd3yCW{mttk#chMl9f`wUlK~`f4S_Cy*K>FCCdAM5T@SP017_1(P907'
        '%FD<pyU2>@?>pU3DnRtR|Gm%d9BdZoK^)!G;Pm}ZHl-iWH(m2Dn?cQTfIm;D=613&2)P7Mi8e#B>T;+8vfVj#)WL(XXw++e'
        '&KZp^0`@}UK%{5=Yi_aVtk^~>4-'
        '9|W^eWU@t{~5*IrhfO5fv4Gu(9(ZY+0PJt~YDz1T2EM=<7A46W+v8i~ipt4A#<K&mq__rQ;NfUjuMt(YL-'
        'wd#i`37zVhWz|dXf$QA>DN;DKqP0FOuSMoBQZ$Zh5yyTcWSBxBeQ$PMh8Y-k4qZU};nAt$9ivxCAA-V-'
        'h<FZXHL)@G=>`WME}C<s+n(Yo;j#m2S=I1!mQDl7{W6bX`JyPY@nA68`PLo6ztMO!xEZl0dF>#E0Stnq^bw8*!VgMRpO)4i'
        'uD%uc`jrV_1!!bxBJur*wg-@V<6Y95-'
        'qqG6A^`I$pl*&4cPQ1nbV~`9hk_(|p`H!4L*~HjE#|SU_T!vwtE(HH+<@VH*}wP*95f#e$JC?`@C)Sn@$~Z{m~)Z$F?2$fB'
        ';NeB?&JNF52zXU$BXydZ|lPRaCG4xoSwWt`rx0Op8s-'
        'warhY~g1qY^AC6Cd*gr;gB7QSS$~e~J7l&t`k52ZF{V!*ypTAtdjG-'
        'RcKmYV)|NI=+;EU%Bg%3mL=O3RM!c{nYaQgZ4{>i)M@T0z0npu#rASTZ#7Rwrt`<r!2z*t%_zJ%*XczooRc+H?*x*@vnsz-'
        'UpN;vodU@EIjrYRIUFpR!qWg_EluLjWTxhjkW1m6gH*@WkL#}M?gUZaYF$+|GQ)rFCw;K0=N0q7jJE&^m37oN`Dc=nsEN&r'
        'rH8|lR6py9xO2Z)HIp5xyg{&e!=@v)<}_4Mv)FHik(3bEukxI1*h1rHk+ays@aMqGtaFmE9g!o#(aQtiPVXW%K@X-'
        'hw{cO?n+{fg>UWflMgifYgdAXDRcTcs{!V?AN1JKB(Eg|&J(f5xWfzDJ!HR6}27I7t(QrqKjV<j3H)%1-'
        '$196_u^*Hhi#eB0ZUv#tza(4f<6kx>iR)5;e62is~Sv?f7}=6IJ}5mvcbcQdS10CEs;YfY(|I0zgt`>7{cP3$i}7r(|gOih'
        'FcAWttmj7}9E8-xK^9vjq-'
        '+J(>tn#?oQc$=rm++5~VuM3bYWn1>KkOgZCHcxM%!ptdz=TuVC+Lr8?b9qJguDKdeG>^b!J?|OXysAIF;f?^OMwN)6eL1{p'
        '?`U_9L#mt7=rbf!CUn0pD$f#Yr8jB_c&R!zP&K%Ye9sRTK{BrhtJ{itoK!t*qVF-'
        '*;l4(nh$i%9$AC4Js6!$kHL#dv;2T>Xk5({Fe<P#)LN#8MDr?Wgt;e~JsruEfmOrbljf*gnW4)SP!P82ZP|&`5Rka)~NW`9'
        '%`HY_x3RXs;A)c`UYCb+{M1j#d@>Yx}0XCj}^(4C<zBht+UIcij)u<{eed%1$0oKhv^?Fy$?)88b-'
        '$%XszUMLD#|t#yXDT0J;O{kV{wSugfk<#mUA3Am>&c*9*3(to9)4@9`?A0BZJ@Oqj(WA-;6N$NEdq!HUzzRH(i83I=-'
        'HJon0TvG)FJ)B&{hLA??B9$0rh<=y)l5*4D{C!mAo-B-OD=WX?^jq!V+}c^Lq*C^zcp;D;L&Arp`{+fmQ0QWxxLbg~_}g'
    ),
    'native_package_mount.py':(
        'c-qZc?OWV7692w`g~HuaughXy3Te93JV(i<&4mCDkh}Kio*!dtSp~1<$MO=k9RK^xj3iq&8z9YnI6h=8X*`-'
        'Y&HOaZ%F4>Al+~INDQ7ovCepYPGG}taF6BH=dCFudu0$SZtjOl@z^amSwaaqJ;v@l@iX|mad0vS)Q$eqHKIJT)S5urx;tDt'
        'vCBG5;6a31AE5+2jC^EqnE8^rjzT)h1QE@e3Iltj0n@Ux|N;OphJD&>Go5^&ZaV8W?c_uD-'
        '2~4vEQ&kG1HJRhHWUwJ_a3d7RFY+sPS;}i<7b_MsHH)*1C7D$7k_Ww&m6hJ4lrt7ZlX(SGQN+ZokY&Z<JeL(2LG{cOu$aPD'
        'cKA`r+&)XC`JlW&Vws%4(2|tgJ}Kba>%HGUeK$HkIov-Ov5<LS6Isc%LgYD5*WyBWy|dBb=-'
        '~YA@lo{CZ|9>kpxWGe#lB^m8(Z7vr}utzzW;jve7`2y>JYqndpN2WUv76_-'
        '~Tm23S4}(`NI}XyncHM`yQXZ)qB4D?v*Ay**|!<|FdD#JNV_p(YuE8&dzr)dml~?kN00kXQPATqt`gSz0-'
        'roH*bHg3BR)f_M%@#zrqCgA(68}WV~N`|GtRVCL3!%jPG7<-~ZL~d(qMU`_VG0Kla!Dg@nyx6s(Q!HU^tp-_x7DB#R-'
        ')PIPR&#|Ryjr7ZjZh_gATM}OF3@K*p`ub1)(<9Q+>*84X|VpdBIfsvE9gLFPClwKV`u$5JG%@-;>FKNt+7-'
        'CYED(rg$Z@|2v=MR7<##$7sM2PSWoDCp_Ae5h?JkIrmALxzyz7``DS6o%gq%a{EsA;^l^RnNT*AJ%r*1T&tfkc&RF%lCvvK'
        '5>nkwYBj39r?uN*@k6%XqG*YN33#$F?@MUugy;O)L~&Vp>n+lsqDHq9Z&H;0vvRnrBt0St~gBRo`1TYakSEt4aJlh))nC&K'
        'G?Y^%e)W3{uXW7l2`dz`MsO@Auz5oW0e9jb?znZX5A|-v*mlCT5ebRfgQ5d1+IdtjVJpuVQ$Jtzj{4L6-'
        'rOpK@M4;f7Qs6}dri8vrlDJJ0X{?THEeUhy0=Amk?)D4y{IpCcU@FlB^R-'
        'u<7@wC30Nhh({^1sT5x%_9nH7PV}!)Cp6XZ~Wng=`)0@g-'
        'T=r))kP$t7O_pRxJu+y$jbWV;@Te85wF*Bcx06<Hnybj>Et=`9qsJG&|I$AAiQxmSB(xrM-'
        ';=cGgC)9s7H3^8H_UGgaZmc$bQHm<q^O6_mOx+@MuFD7GNOaj{Y~`+ioRcJP`>3SW^zL_$$lI$RMtG?6-'
        'iq^{r>Bmpr2X0(I$i&V8|&uD7(nFyYxs7=2uir?PmvzlEHgYm%TNTj056int>M)BAoC9?KZJA<`%{vsvwc*Y$C!JR=|6g*F'
        'B8BoiyV*<2rSTAaWk9Qq|H;b+p#*g@%n6v}fS#`K*n&GpxgE2rxSuPwEQDcnRmQzK=XJ}KB<S9oYaXUV7hJusQI^<CC2agD'
        'W;=l$czvaoiiZ3$`E_=2PD3T+-P7+v&anlRE<JjLNwXE-'
        '2OI>X!$7u>j&Ic(DA2)ss9}@l;Iyevr9e$364Zlk%vn6gdjJ0t(#{tlsa~j%|{#Qi%5?fNbT3w=MD?ZS=7#~h74z(9>#c#z'
        'nI^so$hz`&cMhDIKq@DAAi^Q4t>!{N2ebZh4y6^|M7*U??vR$e#iLmNw-'
        '44WXnGuX35OV=43qDE#1?Xa!LrCcOs)f*d5t54YE8gE6P_gp;N0mgdknxH?swW8G1qc#u*YqWGUH=3KzC9}l={I~5F3jRwO'
        'aNmbVt@#~YVT9$JGT}Aaxu8%&Lqj9PIIlSICKX0{?mfc4jtHne^h^0L$n_4;$uQqdg6hTL1f7Dt@wknjypOId)?b2EYJ>n%'
        '^gn%v)b!i@}5?q!IznsiOML!Zwr{^X=5~t4u=-'
        '=8b~;71H=}39WKFTg9qt?{u*R8ynyJWDmQ>%Th3|Fa;UH2j0<GO_gzf;06jw-'
        'SlzzFHXR?EI|6N@uLusQy(839CPh*g0axhu3SiK-BNP@dm#<^(nn_@$G*+&y->lO_bSd%_PDI-'
        'c4N)`O_QOL%IWKkB2ClOnxRW5^un0ujfH4@F$2LNQd1tsJH0)Wu&_?P{wAUx(^?(+~og<8xq+bN?F}rU<rO~Ph8tuWT<Z&8'
        'dI_hJkZZ2~;3b3)$_u(hPqO^xyj&Jdwr-LYw`9xf`PFGbfhHmd6lX0qO*MNkek_Wdba}*O^RLtpZ3_j!nQ>kfmJiGpANjzU'
        'i;*rcv?B);<H+H#SN?8q<jbdYlm6erIjzFU66{>5IWoj2wMJ5taWednaifFLOw_um^Jk5B@-XENR*-'
        'MxUdc^FA_Os2#Lc;3{YU1UReK<X2YJRCIQO(Is>;yZ0DKD`XiLFI|F{T~Evjy?Hf@+0L$ZA^3`PG!gy1`iTEMDw77as6omg'
        '(lFs2~s~`c2$7=YiEz-}^{572e%1yEs|nxc9-P5@SS%*d8QP`s+?NaJ_}%n^B$RY9_YCg-%!^<Ml_XsQ(D2xBMEQY-'
        'b=RRDDlhJYIw?0SWn3Wxq{Y-'
        '>_WrT7zfa?v|DEZWa9tdlL)Y(gp7#gkd@V#7k<s@CrM?_I0d>a1GR_8=#J?ra*`13Zu?3i!~zb4W2jVQ=T92TgV%5=ul@v)'
        'Us_&@Jt2gIrhM*q(@z49UW&$5w^_>GiP*KRNJAhGn0Y<dRr8jr9M{Jpgcl@Fb`>ECU1CA@`<?Zd%-$5?*Txx?^`f-'
        'H?emXC+Bu9n)lBDYrys)PP4jq!`lkcQ1VR6h6f*W7I-(U-H5t%{j-'
        'd%OWAAdZ28iA=DdIA41w9{_zb_y>q+5ksxzSqQJ${92k`vZAvW(eXV~^p9h&P$I~mu@ZuB>XA$s|GaHGp4?G)fW3@g#s1bs'
        '_=bKovod1lr7o44wu$uV5rIh7kg64Zf5D@8^qZ+M5<YUt2VtNp%pRjSc6z2o2CyWMEpTsPAA{+|0O<4@IeQ_l@Y3ku^*7lC'
        '12Ml3E_44}**WFOE`LXo8kKE>I!uE3QnQh;>kE5dN;b#O_0gc1N1pL!#I9qAuX1<7<K)Bc9s+1XhFb6|~svmrM(H{k?=H8$'
        'k-'
        '_IBgP&K52+=jxh<pcBB==GL<qz|3)U`uh0j@VEcav%RkT<~t>HF@&{=TI5N8lDh3KfF2nwQdyuy^L>ZD#mvxKQ5BbXwr*rL'
        'pYaC59MRXuiNsGd3>D1Q5=az?9Ws$$EB0y1bNrSGi4&{KYnfaF$OeD_*%<)_u?;LpNV!T%QGiypnq(HU?YgHcHQJPWkLaz3'
        'N5^lD4-'
        'b$3*+9glKJR<BW(~X~PNuxu=`^;RU4zM~6EHwc!wvcU&JG;(G6JtOlD9Lm!J2O%u11tjDei~#70snfrXp*6OPf4by{AN|J?'
        'jwcI+mubU0{5CriFZdBiRX&78qU{V6L1LXsB|e<h+E;NhcL?CIZ?f&VIp`bn_yZD;5-'
        '2*cVYOE{Zk0tpQ#kHcTyMI{rwOlIz0)5%^fB(cWJXRFo++LZ~%Z8gWQ?F<jg1K)e5o80cgf5)~wUD2d(Lj*fmhJU)2$m>?p'
        'b$dHl{=0rCMnZk_o!CCb7>~!>VAE|BHq#A1{a`fzraC5j^L*Tpn{I5L}E>1NyMF*u!$p90$beETg;_+CfV6NmWi>Qs!`jQM'
        '`cYwKzttF-vq}ciK`$OIFfJ@)s&yJ6ny!?o0JCT{pA*xGGW+6IA7~(3QVYt^Z+{UoZH_>5NAMegv#cwGDVx6z^*L>lxdZD-'
        'Kh)unI4PE$RU_Tt_Ptg{=ta|ret1(n#z9&uJx*NAkefPf&_`UxER1}(='
    ),
}


def module(name,root=DEPLOY):
    spec=importlib.util.spec_from_file_location('fixture_'+name,root/(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def write(path,value):
    path.write_text(json.dumps(value,sort_keys=True)+'\n');return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture_file(u,path,digest=None):
    assert path.is_file()and not path.is_symlink()and path.stat().st_nlink==1
    if digest is not None:u.require(u.digest(path)==digest,'source_checksum_changed')


@pytest.fixture
def prepared(tmp_path,monkeypatch):
    m=module('upgrade_catalogue');u=module('upgrade_custom',VIDEO/'deploy/native-executor')
    root=tmp_path/'installed';root.mkdir();state=tmp_path/'state';state.mkdir();folder=tmp_path/'bundle';folder.mkdir()
    monkeypatch.setattr(u,'DEST',root);monkeypatch.setattr(u,'STATE',state)
    for name in u.NAMES:
        source=VIDEO/'deploy/native-executor'/name if name in {'broker_service.py','installer-sources.json'}else layout.source(Path(name).stem)
        if name in HISTORICAL_COMPAT_SOURCES:
            historical=zlib.decompress(base64.b85decode(HISTORICAL_COMPAT_SOURCES[name]))
            assert hashlib.sha256(historical).hexdigest()==m.EXPECTED_SOURCES[name]
            (root/name).write_bytes(historical)
        elif name=='native_executor.py':
            # The later rg image upgrade changed only IMAGE. Reconstruct this
            # fixture's reviewed pre-upgrade bytes, then verify the original pin;
            # never update historical commissioning expectations from HEAD.
            current=source.read_bytes()
            lines=current.splitlines(keepends=True)
            image_lines=[i for i,line in enumerate(lines)if line.startswith(b'IMAGE = ')]
            assert len(image_lines)==1
            lines[image_lines[0]]=f'IMAGE = "{u.IMAGE}"\n'.encode()
            historical=b''.join(lines)
            assert hashlib.sha256(historical).hexdigest()==m.EXPECTED_SOURCES[name]
            (root/name).write_bytes(historical)
        else:shutil.copyfile(source,root/name)
    previous={'schema_version':1,'profile':u.INSTALL_PROFILE,'image':u.IMAGE,'runtime':u.RUNTIME,
        'sources':{n:m.EXPECTED_SOURCES[n]for n in u.INSTALL_NAMES},'proof_sha256':'historical-installer-proof','at':'original-install'}
    custom={'schema_version':1,'profile':u.PROFILE,'image':u.IMAGE,'runtime':u.RUNTIME,
        'sources':{n:m.EXPECTED_SOURCES[n]for n in u.CUSTOM_NAMES},'proof_sha256':'original-custom-proof'}
    write(root/'installer-commissioning.json',previous);write(root/'custom-commissioning.json',custom)
    unit=tmp_path/'unit';unit.write_text('unchanged-unit');monkeypatch.setattr(u,'UNIT',unit);monkeypatch.setattr(u,'UNIT_SHA',u.digest(unit))
    for target,source in [('installer-sources-50.json',DEPLOY/'installer-sources-50.json'),
        ('plugin_external_pins.json',VIDEO/'runtime/plugin_external_pins.json'),
        ('external-plugins-20261005.json',VIDEO/'deploy/native-executor/external-plugins-20261005.json'),
        ('catalogue-release.json',DEPLOY/'installer-catalogue-release.json')]:shutil.copyfile(source,folder/target)
    if os.geteuid()!=0:
        monkeypatch.setattr(u,'root_file',lambda p,digest=None:fixture_file(u,p,digest))
        monkeypatch.setattr(u,'canonical_bundle',lambda p:p.resolve(strict=True))
    calls=[]
    def run(argv):
        calls.append(argv)
        if argv[:2]==['systemctl','is-active']:return 'active'
        if 'image'in argv:return u.IMAGE
        return ''
    monkeypatch.setattr(u,'run',run);monkeypatch.setattr(u,'service_pids',lambda:{'application':'17','media':'18','docker':'19'})
    for relative,table,terminal in [('leases.sqlite3','leases','stopped'),('installer/jobs.sqlite3','jobs','released'),('custom/jobs.sqlite3','jobs','released')]:
        path=state/relative;path.parent.mkdir(exist_ok=True)
        with sqlite3.connect(path)as db:db.execute('CREATE TABLE '+table+'(status TEXT,value TEXT)');db.execute('INSERT INTO '+table+' VALUES(?,?)',(terminal,'keep private fixture state'))
    record,rows=m.bundle_record(u,folder)
    return m,u,folder,record,rows,previous,custom,calls


def test_real_reviewed_release_is_additive_exact_original_sources(prepared):
    m,u,folder,record,rows,previous,custom,_=prepared
    assert m.installed(u,record,rows)==(previous,custom)
    old={r['plugin_id']:r for r in json.loads((u.DEST/'installer-sources.json').read_text())}
    new={r['plugin_id']:r for r in rows}
    assert len(old)==47 and len(new)==50 and set(new)-set(old)==m.ADDED_IDS
    assert all(new[k]==v for k,v in old.items())
    for pin in json.loads((folder/'plugin_external_pins.json').read_text())['packages']:
        assert new[pin['id']]['revision']==pin['revision']


@pytest.mark.parametrize('change',['source_bytes','duplicate_id','prior_source','prior_ids','ids','sources','missing_id','extra_id','proof_worker','proof_script_exec','proof_scope','pins','custom_source','unit','closure'])
def test_bad_source_proof_or_installed_closure_rejected_before_service(prepared,change):
    m,u,folder,record,rows,previous,custom,calls=prepared
    if change=='source_bytes':(folder/'installer-sources-50.json').write_text('tampered')
    elif change=='duplicate_id':rows[1]=rows[0];record['files']['installer-sources-50.json']=write(folder/'installer-sources-50.json',rows)
    elif change=='prior_source':rows[0]={**rows[0],'subtree':'other'}
    elif change=='prior_ids':record['previous_ids_sha256']='f'*64
    elif change=='ids':record['ids_sha256']='f'*64
    elif change=='sources':record['sources_sha256']='f'*64
    elif change=='missing_id':rows.pop();record['files']['installer-sources-50.json']=write(folder/'installer-sources-50.json',rows)
    elif change=='extra_id':rows.append({**rows[0],'plugin_id':'extra'});record['files']['installer-sources-50.json']=write(folder/'installer-sources-50.json',rows)
    elif change in {'proof_worker','proof_script_exec','proof_scope'}:
        proof=json.loads((folder/'external-plugins-20261005.json').read_text())
        if change=='proof_worker':proof['worker_sha256']='0'*64
        elif change=='proof_script_exec':proof['upstream_scripts_executed']=1
        else:proof['packages'][0]['source']['subtree']='other'
        record['files']['external-plugins-20261005.json']=write(folder/'external-plugins-20261005.json',proof)
    elif change=='pins':
        pins=json.loads((folder/'plugin_external_pins.json').read_text());pins['packages'][0]['revision']='a'*40
        record['files']['plugin_external_pins.json']=write(folder/'plugin_external_pins.json',pins)
    elif change=='custom_source':(u.DEST/'custom_worker.py').write_text('changed')
    elif change=='unit':u.UNIT.write_text('changed')
    elif change=='closure':record['closure_sha256']='a'*64
    write(folder/'catalogue-release.json',record)
    with pytest.raises(RuntimeError):
        if change=='prior_source':m.installed(u,record,rows)
        else:
            record,rows=m.bundle_record(u,folder);m.installed(u,record,rows)
    assert not any(c[:2]in [['systemctl','stop'],['systemctl','start']]for c in calls)


def test_commissioning_preserves_historical_proof_and_all_worker_hashes(prepared):
    m,u,folder,record,rows,previous,_,_=prepared
    updated=m.commissioning_record(u,previous,record,'new',Path('/root/rollback'))
    assert updated['proof_sha256']==previous['proof_sha256']and updated['at']==previous['at']
    assert updated['sources']['installer-sources.json']==record['files']['installer-sources-50.json']
    assert all(updated['sources'][k]==v for k,v in previous['sources'].items()if k!='installer-sources.json')


@pytest.mark.parametrize('boundary',['native','installer','custom','container'])
def test_shared_nonterminal_or_container_blocks_catalogue_change(prepared,boundary,monkeypatch):
    m,u,folder,record,rows,_,_,_=prepared
    paths={'native':('leases.sqlite3','leases'),'installer':('installer/jobs.sqlite3','jobs'),'custom':('custom/jobs.sqlite3','jobs')}
    if boundary=='container':monkeypatch.setattr(u,'run',lambda argv:'rocoto-native-fixture')
    else:
        relative,table=paths[boundary]
        with sqlite3.connect(u.STATE/relative)as db:db.execute('UPDATE '+table+" SET status='uncertain'")
    with pytest.raises(RuntimeError,match='active_|container_present'):u.quiescent()


def prepare_main(prepared,monkeypatch,*,apply=True,fail=False):
    m,u,folder,record,rows,previous,custom,calls=prepared
    monkeypatch.setattr(m,'load_common',lambda:u)
    monkeypatch.setattr(m.os,'geteuid',lambda:0);monkeypatch.setattr(m.socket,'gethostname',lambda:'rocoto-staging')
    monkeypatch.setattr(m.argparse.ArgumentParser,'parse_args',lambda _:SimpleNamespace(bundle=folder,apply=apply))
    control=folder/'control.sock';control.touch();monkeypatch.setattr(u,'CONTROL',control)
    def ready(common,actual,values,custom_hash):
        assert actual==record and values==rows and u.digest(u.DEST/'custom-commissioning.json')==custom_hash
        if fail:
            with sqlite3.connect(u.STATE/'installer/jobs.sqlite3')as db:db.execute("INSERT INTO jobs VALUES('released','accepted after restart')")
            raise RuntimeError('synthetic_readiness_failure')
        return {'installer':{'supported_plugin_ids':sorted(r['plugin_id']for r in rows)},'custom':{'available':True}}
    monkeypatch.setattr(m,'ready',ready)
    return m,u,record,rows,previous,custom,calls


@pytest.mark.skipif(os.geteuid()!=0,reason='real root atomic ownership and local rollback rehearsal')
@pytest.mark.parametrize('fail',[False,True])
def test_root_rehearsal_updates_only_two_files_preserves_databases_and_rolls_back(prepared,monkeypatch,capsys,fail):
    m,u,record,rows,previous,custom,calls=prepare_main(prepared,monkeypatch,fail=fail)
    before={p.name:p.read_bytes()for p in u.DEST.iterdir()}
    data={name:u.database_contents(u.STATE/name)for name in u.DATABASES}
    oldmask=os.umask(0o077)
    try:
        if fail:
            with pytest.raises(RuntimeError,match='synthetic_readiness_failure'):m.main()
        else:m.main()
    finally:os.umask(oldmask)
    after={p.name:p.read_bytes()for p in u.DEST.iterdir()}
    if fail:assert before==after
    else:
        assert {k for k in before if before[k]!=after[k]}==m.CHANGED
        assert json.loads(after['installer-sources.json'])==rows
    assert u.database_contents(u.STATE/'leases.sqlite3')==data['leases.sqlite3']
    assert u.database_contents(u.STATE/'custom/jobs.sqlite3')==data['custom/jobs.sqlite3']
    with sqlite3.connect(u.STATE/'installer/jobs.sqlite3')as db:
        values=db.execute('SELECT value FROM jobs').fetchall()
    assert values==[('keep private fixture state',),('accepted after restart',)]if fail else values==[('keep private fixture state',)]
    assert all(c[2]==u.SERVICE for c in calls if c[:2]in [['systemctl','start'],['systemctl','stop']])
    assert json.loads((next(u.STATE.parent.glob('rollback-catalogue-*'))/'upgrade-evidence.json').read_text()).get('rolled_back',False)is fail


def test_validation_never_stops_services_or_changes_catalogue(prepared,monkeypatch,capsys):
    m,u,record,rows,previous,custom,calls=prepare_main(prepared,monkeypatch,apply=False)
    before={p.name:p.read_bytes()for p in u.DEST.iterdir()};oldmask=os.umask(0o077)
    try:m.main()
    finally:os.umask(oldmask)
    assert json.loads(capsys.readouterr().out)['apply']is False
    assert before=={p.name:p.read_bytes()for p in u.DEST.iterdir()}
    assert not any(c[:2]in [['systemctl','start'],['systemctl','stop']]for c in calls)


def test_existing_selected_package_snapshot_and_generation_survive_additive_catalogue(tmp_path,package):
    async def check():
        old=catalogue();old['plugins']=[p for p in old['plugins']if p['id']not in module('upgrade_catalogue').ADDED_IDS]
        fake=Fake(package);store=NativePackageStore(tmp_path/'state',catalogue_snapshot=old,materializer=fake)
        first=await store.install('notion',principal_id='alice');selection=store.snapshot(principal_id='alice')
        newer=NativePackageStore(tmp_path/'state',catalogue_snapshot=catalogue(),materializer=fake)
        assert await newer.install('notion',principal_id='alice')==first and fake.calls==1
        assert newer.validate_snapshot(selection,principal_id='alice')==selection
        assert newer.snapshot(principal_id='alice').generation==selection.generation
    asyncio.run(check())


def test_actual_broker_protocol_reports_only_its_commissioned_47_or_50_ids(tmp_path):
    async def check():
        import tempfile
        rows=json.loads((DEPLOY/'installer-sources-50.json').read_text());old=json.loads((VIDEO/'deploy/native-executor/installer-sources.json').read_text())
        for sources in (old,rows):
            with tempfile.TemporaryDirectory(prefix='catipc-')as temporary:
                root=Path(temporary);host=fixture_broker(root);backend=Backend(root/'installer-volumes')
                worker=VIDEO/'runtime/native_install_worker.py'
                installer=Installer(host,allowed_sources=sources,backend=backend,worker_path=worker)
                server=native_executor.BrokerServer(host,root/'control.sock');await server.start()
                try:
                    scope=NativeScope('company','alice','readiness');client=InstallerClient(server.path)
                    metadata=await client.capabilities(scope)
                    assert metadata['supported_plugin_ids']==sorted(s['plugin_id']for s in sources)
                    company=root/'company';store=NativePackageStore(company,materializer=BrokerPluginInstaller(company,client=client))
                    probe=NativeInstallerReadiness(company,enabled=True,client=client)
                    assert (await probe.probe(store,principal_id='alice'))['installable_ids']==metadata['supported_plugin_ids']
                    assert not backend.started and installer.live_count()==0 and host.live_count()==0
                finally:await server.close()
    asyncio.run(check())


@pytest.mark.parametrize('change',[None,'only47','custom_worker','image','revision'])
def test_final_probe_binds_complete_installer_and_unchanged_custom_runtime(prepared,monkeypatch,change):
    m,u,folder,record,rows,previous,custom,_=prepared
    # Socket/provider fixture is scalar only; actual UDS capability transport is
    # independently exercised above without any worker allocation.
    monkeypatch.setattr(u,'CONTROL',SimpleNamespace(lstat=lambda:SimpleNamespace(st_mode=0o140660,st_uid=0,st_gid=99)))
    monkeypatch.setattr(u.pwd,'getpwnam',lambda name:SimpleNamespace(pw_gid=99))
    installer={'schema_version':1,'available':True,'image':u.IMAGE,'profile':u.INSTALL_PROFILE,
        'catalogue_revisions':[m.CATALOGUE_REVISION],'supported_plugin_ids':sorted(r['plugin_id']for r in rows),
        'worker_sha256':m.EXPECTED_SOURCES['native_install_worker.py']}
    custom_cap={'schema_version':1,'available':True,'profile':u.PROFILE,'image_digest':u.IMAGE,
        'worker_sha256':m.EXPECTED_SOURCES['custom_worker.py']}
    if change=='only47':installer['supported_plugin_ids']=[p for p in installer['supported_plugin_ids']if p not in m.ADDED_IDS]
    elif change=='custom_worker':custom_cap['worker_sha256']='a'*64
    elif change=='image':installer['image']='sha256:'+'b'*64
    elif change=='revision':installer['catalogue_revisions']=['c'*40]
    monkeypatch.setattr(u,'capabilities',lambda op:installer if op=='plugin_capabilities'else custom_cap)
    (u.DEST/'installer-sources.json').write_bytes((folder/'installer-sources-50.json').read_bytes())
    if change:
        with pytest.raises(RuntimeError,match='readiness_failed'):m.ready(u,record,rows,u.digest(u.DEST/'custom-commissioning.json'))
    else:assert m.ready(u,record,rows,u.digest(u.DEST/'custom-commissioning.json'))=={'installer':installer,'custom':custom_cap}
