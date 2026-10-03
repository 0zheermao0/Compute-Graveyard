import { X, HelpCircle, Cpu, Terminal, FileText, Code, HardDrive, Trash2, Activity } from "lucide-react";

interface InstructionModalProps {
    onClose: () => void;
}

export default function InstructionModal({ onClose }: InstructionModalProps) {
    return (
        <div className="modal-overlay" onClick={onClose}>
            <div className="modal instruction-modal" onClick={(e) => e.stopPropagation()}>
                <div className="modal-header">
                    <h2><HelpCircle size={20} style={{ verticalAlign: 'middle', marginRight: '8px' }} /> 使用快速指引</h2>
                    <button className="btn btn-ghost" onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                <div className="modal-body custom-scrollbar" style={{ maxHeight: '70vh', overflowY: 'auto', paddingRight: '1rem' }}>

                    <section className="ins-section">
                        <h3><Cpu size={18} /> 1. 如何申请资源</h3>
                        <p>在 <strong>资源看板</strong> 页面点击右上角的 <strong>“申请 GPU 容器”</strong> 按钮。在弹窗中选择你需要的 GPU 卡号（支持多选）和租期。申请成功后，系统会自动为你创建并启动容器。</p>
                    </section>

                    <section className="ins-section">
                        <h3><Terminal size={18} /> 2. 如何通过 SSH 连接</h3>
                        <p>申请成功后，在 <strong>我的容器</strong> 页面可以找到你正在运行的容器。点击 <strong>“复制 SSH 命令”</strong>，然后在你本地电脑的终端（CMD, PowerShell 或 Terminal）中点击右键粘贴并回车。初次连接需输入初始密码（同样在页面可见）。</p>
                    </section>

                    <section className="ins-section">
                        <h3><FileText size={18} /> 3. 如何查看和传输文件</h3>
                        <ul>
                            <li><strong>在线查看：</strong>在 <strong>我的容器</strong> 点击 <strong>“工作区”</strong> 按钮，可以使用内置的在线代码编辑器直接查看和编辑文件。</li>
                            <li><strong>本地传输：</strong>建议使用 <strong>WinSCP</strong> 或 <strong>FileZilla</strong>。连接协议选择 SFTP，主机名为服务器 IP，端口和密码与 SSH 一致。你的个人数据持久化保存于 <code>/workspace</code>。</li>
                        </ul>
                    </section>

                    <section className="ins-section">
                        <h3><Code size={18} /> 4. 如何使用 Code-Server (在线 VSCode)</h3>
                        <p>系统默认在容器内预装了 Web 版 VSCode。在 <strong>我的容器</strong> 页面，点击 <strong>“打开 VS Code”</strong> 按钮，即可在浏览器中通过熟悉的 VSCode 界面进行开发。环境已经配置好 Conda ,创建好的环境会持久化在个人目录中，后续申请使用不会丢失。</p>
                    </section>

                    <section className="ins-section">
                        <h3><HardDrive size={18} /> 5. 磁盘占用限制</h3>
                        <p>个人工作区默认配额为 <strong>100 GiB</strong>（具体以“工作区”页面显示为准）。使用量达到配额后将无法申请新容器；连续超限满默认 <strong>24 小时</strong>后，运行中的容器会自动停止。请及时清理工作区文件；停止超过 24 小时的容器会被自动清理，工作区数据仍保留。</p>
                    </section>

                    <section className="ins-section">
                        <h3><Trash2 size={18} /> 6. 容器销毁与数据保存</h3>
                        <p>容器租期到期或被手动销毁后，容器内未保存到 <code>/workspace</code> 的数据会丢失。请提前将代码和重要文件保存到 <code>/workspace</code>；容器销毁后该目录仍会保留。</p>
                    </section>

                    <section className="ins-section">
                        <h3><Activity size={18} /> 7. GPU 长时间低占用自动回收</h3>
                        <p>启用自动回收后，GPU 长时间处于低占用状态的容器将被自动回收（默认 <strong>24 小时</strong>，具体以管理员设置为准）。回收前会发送预警，<code>/workspace</code> 中的数据会保留，CPU 容器不受影响。</p>
                    </section>

                    <div className="modal-hint" style={{ marginTop: '1.5rem', background: 'rgba(52, 211, 153, 0.1)', borderColor: 'rgba(52, 211, 153, 0.2)', color: '#34d399' }}>
                        提示：所有在 <code>/workspace</code> 目录下的修改都会持久化保存。即使容器到期销毁，下次租用时你的代码和数据依然存在。
                    </div>
                </div>

                <div className="modal-actions">
                    <button className="btn btn-primary" onClick={onClose}>知道了</button>
                </div>
            </div>
        </div>
    );
}
